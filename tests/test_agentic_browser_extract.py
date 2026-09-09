"""Operator-level semantic status for agentic browser extraction."""

from __future__ import annotations

import json


def test_blocked_page_is_structured_failure_and_nonzero(monkeypatch, capsys):
    from scripts import agentic_browser_extract as helper

    async def fake_extract(urls, limit_chars):
        return [
            {
                "url": urls[0],
                "title": "Access denied",
                "content": "Access to this page has been denied.",
                "raw_content": "Access to this page has been denied.",
            }
        ]

    monkeypatch.setattr(helper, "_extract", fake_extract)
    monkeypatch.setattr(
        helper,
        "_apply_browser_tool_fallback",
        lambda urls, results, limit_chars: results,
    )

    exit_code = helper.main(["https://thehill.com/example"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert payload["ok"] is False
    assert payload["results"][0]["content_class"] == "challenge"
    assert payload["results"][0]["status"] == "blocked"
    assert payload["results"][0]["blocker"] == "anti_bot_challenge"
    assert payload["results"][0]["fallback_attempted"] is True


def test_article_page_is_structured_success(monkeypatch, capsys):
    from scripts import agentic_browser_extract as helper

    article = " ".join(["verified article body"] * 40)

    async def fake_extract(urls, limit_chars):
        return [
            {
                "url": urls[0],
                "title": "Article",
                "content": article,
                "raw_content": article,
            }
        ]

    monkeypatch.setattr(helper, "_extract", fake_extract)
    monkeypatch.setattr(
        helper,
        "_apply_browser_tool_fallback",
        lambda urls, results, limit_chars: results,
    )

    exit_code = helper.main(["https://example.org/article"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["ok"] is True
    assert payload["results"][0]["content_class"] == "article"
    assert payload["results"][0]["status"] == "success"


def test_legacy_list_flag_preserves_old_json_shape(monkeypatch, capsys):
    from scripts import agentic_browser_extract as helper

    article = " ".join(["verified article body"] * 40)

    async def fake_extract(urls, limit_chars):
        return [{"url": urls[0], "title": "Article", "content": article}]

    monkeypatch.setattr(helper, "_extract", fake_extract)
    monkeypatch.setattr(
        helper,
        "_apply_browser_tool_fallback",
        lambda urls, results, limit_chars: results,
    )

    assert helper.main(["--legacy-list", "https://example.org/article"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, list)
    assert payload[0]["content_class"] == "article"


def test_missing_provider_results_are_structured_failure(monkeypatch, capsys):
    from scripts import agentic_browser_extract as helper

    async def fake_extract(urls, limit_chars):
        return []

    monkeypatch.setattr(helper, "_extract", fake_extract)

    exit_code = helper.main(
        ["--no-browser-tool-fallback", "https://example.org/missing"]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert payload["ok"] is False
    assert len(payload["results"]) == 1
    assert payload["results"][0]["status"] == "error"
    assert "no result" in payload["results"][0]["error"].lower()


def test_limit_trims_output_without_downgrading_valid_article(monkeypatch, capsys):
    from scripts import agentic_browser_extract as helper

    article = " ".join(["verified article body"] * 80)

    async def fake_extract(urls, limit_chars):
        return [
            {
                "url": urls[0],
                "title": "Article",
                "content": article,
                "raw_content": article,
            }
        ]

    monkeypatch.setattr(helper, "_extract", fake_extract)

    exit_code = helper.main(
        [
            "--no-browser-tool-fallback",
            "--limit-chars",
            "30",
            "https://example.org/article",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["ok"] is True
    assert payload["results"][0]["content_class"] == "article"
    assert payload["results"][0]["content"].endswith("...[truncated]")


def test_policy_block_never_launches_local_browser(monkeypatch, capsys):
    from scripts import agentic_browser_extract as helper

    async def fail_if_called(self, urls, **kwargs):
        raise AssertionError("local browser must not receive a policy-blocked URL")

    monkeypatch.setattr(helper, "check_website_access", lambda _url: {
        "host": "blocked.example",
        "rule": "blocked.example",
        "source": "test-policy",
        "message": "Blocked by website policy",
    })
    monkeypatch.setattr(
        helper.LocalBrowserWebSearchProvider, "extract", fail_if_called
    )

    exit_code = helper.main(
        ["--no-browser-tool-fallback", "https://blocked.example/article"]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    result = payload["results"][0]
    assert result["status"] == "error"
    assert result["blocked_by_policy"]["rule"] == "blocked.example"


def test_provider_exception_becomes_per_url_failure(monkeypatch, capsys):
    from scripts import agentic_browser_extract as helper

    monkeypatch.setattr(helper, "check_website_access", lambda _url: None)

    async def safe(_url):
        return True

    async def explode(self, urls, **kwargs):
        raise RuntimeError("browser crashed")

    monkeypatch.setattr(helper, "async_is_safe_url", safe)
    monkeypatch.setattr(helper.LocalBrowserWebSearchProvider, "extract", explode)

    exit_code = helper.main(
        ["--no-browser-tool-fallback", "https://example.org/article"]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert payload["results"][0]["status"] == "error"
    assert "browser crashed" in payload["results"][0]["error"]


def test_private_network_url_never_launches_local_browser(monkeypatch, capsys):
    from scripts import agentic_browser_extract as helper

    monkeypatch.setattr(helper, "check_website_access", lambda _url: None)

    async def unsafe(_url):
        return False

    async def fail_if_called(self, urls, **kwargs):
        raise AssertionError("local browser must not receive an unsafe URL")

    monkeypatch.setattr(helper, "async_is_safe_url", unsafe)
    monkeypatch.setattr(
        helper.LocalBrowserWebSearchProvider, "extract", fail_if_called
    )

    exit_code = helper.main(
        ["--no-browser-tool-fallback", "http://127.0.0.1/private"]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    result = payload["results"][0]
    assert result["status"] == "error"
    assert "private or internal" in result["error"]
