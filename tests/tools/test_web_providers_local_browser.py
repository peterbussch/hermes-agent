"""Tests for the local browser web extract provider."""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import pytest


class TestLocalBrowserProvider:
    @pytest.mark.parametrize(
        ("content", "expected_class", "expected_status"),
        [
            ("Sign in to continue and view this post", "login_wall", "blocked"),
            ("JavaScript is disabled. Enable JavaScript.", "placeholder", "blocked"),
            ("Access denied by web application firewall. Reference 123", "challenge", "blocked"),
            ("A short but real item", "partial", "partial"),
            ("", "empty", "error"),
            (" ".join(["full article evidence"] * 30), "article", "success"),
        ],
    )
    def test_shared_quality_vocabulary(
        self, content, expected_class, expected_status
    ):
        from plugins.web.local_browser.content_quality import classify_extraction

        quality = classify_extraction({"content": content})

        assert quality["content_class"] == expected_class
        assert quality["status"] == expected_status

    def test_provider_error_never_classifies_as_complete_article(self):
        from plugins.web.local_browser.content_quality import classify_extraction

        quality = classify_extraction(
            {
                "content": " ".join(["apparently complete content"] * 30),
                "error": "connection closed before response completed",
            }
        )

        assert quality["content_class"] == "partial"
        assert quality["status"] == "partial"
        assert quality["blocker"] == "connection closed before response completed"

    @pytest.mark.parametrize(
        ("url", "title", "content"),
        [
            (
                "https://www.realclearpolitics.com/video/example.html",
                "realclearpolitics.com",
                "realclearpolitics.com",
            ),
            (
                "https://www.nytimes.com/2026/08/24/example.html",
                "nytimes.com",
                "nytimes.com",
            ),
        ],
    )
    def test_hostname_only_body_is_a_placeholder(self, url, title, content):
        from plugins.web.local_browser.content_quality import classify_extraction

        quality = classify_extraction(
            {"url": url, "title": title, "content": content}
        )

        assert quality["content_class"] == "placeholder"
        assert quality["status"] == "blocked"
        assert quality["blocker"] == "domain_placeholder"

    def test_the_hill_access_denial_is_a_challenge(self):
        from plugins.web.local_browser.content_quality import classify_extraction

        quality = classify_extraction(
            {
                "url": "https://thehill.com/example",
                "title": "Access denied",
                "content": "Access to this page has been denied.",
            }
        )

        assert quality["content_class"] == "challenge"
        assert quality["status"] == "blocked"
        assert quality["blocker"] == "anti_bot_challenge"

    def test_multiline_akamai_access_denial_is_a_challenge(self):
        from plugins.web.local_browser.content_quality import classify_extraction

        content = "\n".join(
            [
                "Access Denied",
                "You do not have permission to access this resource.",
                "Reference #18.42ac1102.1787661134.1d35d993",
                "The requested URL was rejected. Please consult your administrator.",
                "Diagnostic material follows for the request and incident.",
            ]
        )

        quality = classify_extraction(
            {
                "url": "https://example.org/news/article",
                "title": "Access Denied",
                "content": content,
            }
        )

        assert quality["content_class"] == "challenge"
        assert quality["status"] == "blocked"
        assert quality["blocker"] == "anti_bot_challenge"

    def test_available_checks_deps_and_chromium(self, monkeypatch, tmp_path):
        import plugins.web.local_browser.provider as module
        from plugins.web.local_browser.provider import LocalBrowserWebSearchProvider

        browser = tmp_path / "chrome"
        browser.write_text("")
        monkeypatch.setattr(module, "_has_module", lambda name: True)
        monkeypatch.setattr(module, "find_chromium_executable", lambda: browser)
        assert LocalBrowserWebSearchProvider().is_available() is True

        monkeypatch.setattr(module, "_has_module", lambda name: name != "trafilatura")
        assert LocalBrowserWebSearchProvider().is_available() is False

        monkeypatch.setattr(module, "_has_module", lambda name: True)
        monkeypatch.setattr(module, "find_chromium_executable", lambda: None)
        assert LocalBrowserWebSearchProvider().is_available() is False

    def test_find_chromium_executable_uses_env_override(self, monkeypatch, tmp_path):
        from plugins.web.local_browser.provider import find_chromium_executable

        browser = tmp_path / "chrome"
        browser.write_text("")
        monkeypatch.setenv("LOCAL_BROWSER_EXECUTABLE", str(browser))

        assert find_chromium_executable() == browser

    def test_find_chromium_executable_missing_override_returns_none(
        self, monkeypatch, tmp_path
    ):
        from plugins.web.local_browser.provider import find_chromium_executable

        monkeypatch.setenv("LOCAL_BROWSER_EXECUTABLE", str(tmp_path / "missing"))

        assert find_chromium_executable() is None

    def test_extract_success_uses_async_render_path(self, monkeypatch, tmp_path):
        import plugins.web.local_browser.provider as module
        from plugins.web.local_browser.provider import LocalBrowserWebSearchProvider

        browser = tmp_path / "chrome"
        browser.write_text("")
        monkeypatch.setattr(module, "_ensure_runtime_deps", lambda: None)
        monkeypatch.setattr(module, "find_chromium_executable", lambda: browser)

        async def fake_extract_one(url, executable):
            return {
                "url": url,
                "title": "Example",
                "content": "rendered markdown",
                "raw_content": "rendered markdown",
                "metadata": {"browser": str(executable)},
            }

        provider = LocalBrowserWebSearchProvider()
        monkeypatch.setattr(provider, "_extract_one", fake_extract_one)

        result = asyncio.run(provider.extract(["https://example.com"]))

        assert result[0]["content"] == "rendered markdown"
        assert result[0]["metadata"]["browser"] == str(browser)

    def test_extract_classifies_challenge_page_as_blocked(self, monkeypatch, tmp_path):
        import plugins.web.local_browser.provider as module
        from plugins.web.local_browser.provider import LocalBrowserWebSearchProvider

        browser = tmp_path / "chrome"
        browser.write_text("")
        monkeypatch.setattr(module, "_ensure_runtime_deps", lambda: None)
        monkeypatch.setattr(module, "find_chromium_executable", lambda: browser)

        async def fake_extract_one(url, executable):
            content = "Just a moment... checking your browser. Verify you are human."
            return {
                "url": url,
                "title": "Checking your browser",
                "content": content,
                "raw_content": content,
            }

        provider = LocalBrowserWebSearchProvider()
        monkeypatch.setattr(provider, "_extract_one", fake_extract_one)

        result = asyncio.run(provider.extract(["https://example.com"]))[0]

        assert result["content_class"] == "challenge"
        assert result["status"] == "blocked"
        assert result["completeness"] == 0.0
        assert result["blocker"] == "anti_bot_challenge"

    def test_extract_reports_dependency_error_before_browser_launch(self, monkeypatch):
        import plugins.web.local_browser.provider as module
        from plugins.web.local_browser.provider import LocalBrowserWebSearchProvider

        monkeypatch.setattr(
            module,
            "_ensure_runtime_deps",
            lambda: "Local browser dependencies unavailable: boom",
        )

        result = asyncio.run(
            LocalBrowserWebSearchProvider().extract(["https://example.com"])
        )

        assert result[0]["content"] == ""
        assert "dependencies unavailable" in result[0]["error"]

    def test_ensure_runtime_deps_uses_lazy_install_when_missing(self, monkeypatch):
        import plugins.web.local_browser.provider as module

        calls = []
        fake_lazy_deps = types.ModuleType("tools.lazy_deps")

        def fake_ensure(feature, *, prompt=True):
            calls.append((feature, prompt))

        fake_lazy_deps.ensure = fake_ensure
        monkeypatch.setitem(sys.modules, "tools.lazy_deps", fake_lazy_deps)

        states = iter([False, True])
        monkeypatch.setattr(module, "_runtime_deps_available", lambda: next(states))

        assert module._ensure_runtime_deps() is None
        assert calls == [("search.local_browser", False)]

    def test_extract_timeout_returns_per_url_error(self, monkeypatch, tmp_path):
        import plugins.web.local_browser.provider as module
        from plugins.web.local_browser.provider import LocalBrowserWebSearchProvider

        browser = tmp_path / "chrome"
        browser.write_text("")
        monkeypatch.setenv("LOCAL_BROWSER_TIMEOUT_SECONDS", "0.01")
        monkeypatch.setattr(module, "_ensure_runtime_deps", lambda: None)
        monkeypatch.setattr(module, "find_chromium_executable", lambda: browser)

        async def slow_extract_one(url, executable):
            await asyncio.sleep(1)

        provider = LocalBrowserWebSearchProvider()
        monkeypatch.setattr(provider, "_extract_one", slow_extract_one)

        result = asyncio.run(provider.extract(["https://example.com"]))

        assert result[0]["content"] == ""
        assert "timed out" in result[0]["error"]

    @pytest.mark.parametrize(
        ("exits_after_stop", "expected_wait_calls", "expected_kill_calls"),
        [(True, 1, 0), (False, 2, 1)],
    )
    def test_extract_one_uses_nodriver_and_trafilatura(
        self,
        monkeypatch,
        tmp_path,
        exits_after_stop,
        expected_wait_calls,
        expected_kill_calls,
    ):
        import plugins.web.local_browser.provider as module
        from plugins.web.local_browser.provider import LocalBrowserWebSearchProvider

        browser_path = tmp_path / "chrome"
        browser_path.write_text("")
        start_calls = []

        class _FakeTab:
            async def sleep(self, _seconds):
                return None

            async def get_content(self):
                return "<html><head><title>Example</title></head><body><main>Hello JS</main></body></html>"

        class _FakeBrowser:
            def __init__(self):
                self.aclose_calls = 0
                self.stop_calls = 0
                self._process = _FakeProcess()

            async def get(self, url):
                self.url = url
                return _FakeTab()

            async def aclose(self):
                self.aclose_calls += 1

            def stop(self):
                self.stop_calls += 1

        class _FakeProcess:
            def __init__(self):
                self.wait_calls = 0
                self.kill_calls = 0

            async def wait(self):
                self.wait_calls += 1
                if not exits_after_stop and not self.kill_calls:
                    raise asyncio.TimeoutError
                return 0

            def kill(self):
                self.kill_calls += 1

        fake_nodriver = types.ModuleType("nodriver")
        fake_browser = _FakeBrowser()

        async def fake_start(**kwargs):
            start_calls.append(kwargs)
            return fake_browser

        fake_nodriver.start = fake_start
        fake_trafilatura = types.ModuleType("trafilatura")
        fake_trafilatura.extract = (
            lambda rendered_html, **kwargs: "Hello JS as markdown"
        )
        monkeypatch.setitem(sys.modules, "nodriver", fake_nodriver)
        monkeypatch.setitem(sys.modules, "trafilatura", fake_trafilatura)
        monkeypatch.setattr(
            module, "_BROWSER_EXIT_TIMEOUT_SECONDS", 0.01, raising=False
        )

        result = asyncio.run(
            LocalBrowserWebSearchProvider()._extract_one(
                "https://example.com",
                Path(browser_path),
            )
        )

        assert result["title"] == "Example"
        assert result["content"] == "Hello JS as markdown"
        assert start_calls[0]["browser_executable_path"] == str(browser_path)
        assert start_calls[0]["headless"] is True
        assert start_calls[0]["sandbox"] is True
        assert fake_browser.aclose_calls >= 1
        assert fake_browser.stop_calls == 1
        assert fake_browser._process.wait_calls == expected_wait_calls
        assert fake_browser._process.kill_calls == expected_kill_calls
