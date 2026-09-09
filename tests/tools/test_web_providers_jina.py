"""Tests for the Jina Reader web extract provider."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _reset_jina_health():
    import plugins.web.jina.provider as module

    module._reset_health_for_tests()
    yield
    module._reset_health_for_tests()


class _FakeResponse:
    def __init__(self, text: str = "# Example\n\nHello", status_code: int = 200):
        self.text = text
        self.status_code = status_code
        self.raise_for_status = MagicMock()


def _install_fake_httpx_client(monkeypatch, *, response=None, error=None):
    calls = []

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, url, headers=None):
            calls.append({"url": url, "headers": headers or {}})
            if error is not None:
                raise error
            return response or _FakeResponse()

    monkeypatch.setattr("httpx.Client", _FakeClient)
    return calls


class TestJinaReaderProvider:
    def test_available_without_key(self, monkeypatch):
        monkeypatch.delenv("JINA_API_KEY", raising=False)
        from plugins.web.jina.provider import JinaReaderWebSearchProvider

        provider = JinaReaderWebSearchProvider()
        assert provider.name == "jina"
        assert provider.supports_search() is False
        assert provider.supports_extract() is True
        assert provider.is_available() is True

    def test_extract_success_returns_markdown(self, monkeypatch):
        from plugins.web.jina.provider import JinaReaderWebSearchProvider

        calls = _install_fake_httpx_client(
            monkeypatch,
            response=_FakeResponse("# Example Domain\n\nReadable markdown"),
        )

        result = JinaReaderWebSearchProvider().extract(["https://example.com"])

        assert result == [
            {
                "url": "https://example.com",
                "title": "Example Domain",
                "content": "# Example Domain\n\nReadable markdown",
                "raw_content": "# Example Domain\n\nReadable markdown",
                "metadata": {
                    "sourceURL": "https://example.com",
                    "title": "Example Domain",
                    "provider": "jina",
                },
            }
        ]
        assert calls[0]["url"] == "https://r.jina.ai/https://example.com"
        assert calls[0]["headers"]["X-Return-Format"] == "markdown"

    def test_extract_adds_optional_auth_header(self, monkeypatch):
        from plugins.web.jina.provider import JinaReaderWebSearchProvider

        monkeypatch.setenv("JINA_API_KEY", "test-token")
        calls = _install_fake_httpx_client(monkeypatch)

        JinaReaderWebSearchProvider().extract(["https://example.com"])

        assert calls[0]["headers"]["Authorization"] == "Bearer test-token"

    def test_http_error_returns_per_url_error(self, monkeypatch):
        import httpx
        from plugins.web.jina.provider import JinaReaderWebSearchProvider

        response = _FakeResponse("", status_code=429)
        response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "429", request=MagicMock(), response=response
        )
        _install_fake_httpx_client(monkeypatch, response=response)

        result = JinaReaderWebSearchProvider().extract(["https://example.com"])

        assert result[0]["url"] == "https://example.com"
        assert result[0]["content"] == ""
        assert "HTTP 429" in result[0]["error"]

    def test_request_error_returns_per_url_error(self, monkeypatch):
        import httpx
        from plugins.web.jina.provider import JinaReaderWebSearchProvider

        _install_fake_httpx_client(
            monkeypatch,
            error=httpx.RequestError("connection failed"),
        )

        result = JinaReaderWebSearchProvider().extract(["https://example.com"])

        assert result[0]["url"] == "https://example.com"
        assert "request failed" in result[0]["error"]

    def test_empty_response_is_not_successful_content(self, monkeypatch):
        from plugins.web.jina.provider import JinaReaderWebSearchProvider

        _install_fake_httpx_client(monkeypatch, response=_FakeResponse("   "))

        result = JinaReaderWebSearchProvider().extract(["https://example.com"])

        assert result[0]["content"] == ""
        assert "no content" in result[0]["error"].lower()

    def test_anonymous_auth_failure_opens_cached_cooldown(self, monkeypatch):
        import httpx
        import plugins.web.jina.provider as module

        module._reset_health_for_tests()
        monkeypatch.delenv("JINA_API_KEY", raising=False)
        response = _FakeResponse("", status_code=403)
        response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "403", request=MagicMock(), response=response
        )
        calls = _install_fake_httpx_client(monkeypatch, response=response)
        provider = module.JinaReaderWebSearchProvider()

        first = provider.extract(["https://example.com/one"])
        second = provider.extract(["https://example.com/two"])

        assert len(calls) == 1
        assert "HTTP 403" in first[0]["error"]
        assert "cooldown" in second[0]["error"].lower()
        assert provider.is_available() is False

    @pytest.mark.parametrize("status", [401, 403])
    def test_keyed_auth_failure_opens_immediate_cached_cooldown(
        self, monkeypatch, status
    ):
        import httpx
        import plugins.web.jina.provider as module

        api_key = "test-secret-jina-key"
        monkeypatch.setenv("JINA_API_KEY", api_key)
        response = _FakeResponse("", status_code=status)
        response.raise_for_status.side_effect = httpx.HTTPStatusError(
            str(status), request=MagicMock(), response=response
        )
        calls = _install_fake_httpx_client(monkeypatch, response=response)
        provider = module.JinaReaderWebSearchProvider()

        first_batch = provider.extract(
            ["https://example.com/one", "https://example.com/two"]
        )
        next_call = provider.extract(["https://example.com/three"])

        assert len(calls) == 1
        assert f"HTTP {status}" in first_batch[0]["error"]
        assert "cooldown" in first_batch[1]["error"].lower()
        assert "cooldown" in next_call[0]["error"].lower()
        assert api_key not in first_batch[0]["error"]
        assert api_key not in first_batch[1]["error"]
        assert api_key not in next_call[0]["error"]
        assert provider.is_available() is False

    def test_repeated_rate_limits_open_cooldown(self, monkeypatch):
        import httpx
        import plugins.web.jina.provider as module

        module._reset_health_for_tests()
        response = _FakeResponse("", status_code=429)
        response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "429", request=MagicMock(), response=response
        )
        calls = _install_fake_httpx_client(monkeypatch, response=response)
        provider = module.JinaReaderWebSearchProvider()

        provider.extract(["https://example.com/one"])
        provider.extract(["https://example.com/two"])
        third = provider.extract(["https://example.com/three"])

        assert len(calls) == 2
        assert "cooldown" in third[0]["error"].lower()
        assert provider.is_available() is False

    def test_repeated_transport_failures_open_cooldown(self, monkeypatch):
        import httpx
        import plugins.web.jina.provider as module

        calls = _install_fake_httpx_client(
            monkeypatch,
            error=httpx.RequestError("connection failed"),
        )
        provider = module.JinaReaderWebSearchProvider()

        provider.extract(["https://example.com/one"])
        provider.extract(["https://example.com/two"])
        third = provider.extract(["https://example.com/three"])

        assert len(calls) == 2
        assert "cooldown" in third[0]["error"].lower()
        assert provider.is_available() is False
