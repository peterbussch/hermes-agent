"""Result-aware web provider fallback regression tests."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List

import pytest

from agent.web_search_provider import WebSearchProvider


class _SearchProvider(WebSearchProvider):
    def __init__(self, name: str, response: Dict[str, Any], calls: list[str]):
        self._name = name
        self._response = response
        self._calls = calls

    @property
    def name(self) -> str:
        return self._name

    def is_available(self) -> bool:
        return True

    def supports_search(self) -> bool:
        return True

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        self._calls.append(self.name)
        return self._response


class _ExtractProvider(WebSearchProvider):
    def __init__(
        self,
        name: str,
        responses: Dict[str, Dict[str, Any]],
        calls: list[tuple[str, List[str]]],
    ):
        self._name = name
        self._responses = responses
        self._calls = calls

    @property
    def name(self) -> str:
        return self._name

    def is_available(self) -> bool:
        return True

    def supports_search(self) -> bool:
        return False

    def supports_extract(self) -> bool:
        return True

    async def extract(self, urls: List[str], **kwargs: Any) -> List[Dict[str, Any]]:
        self._calls.append((self.name, list(urls)))
        return [dict(self._responses[url], url=url) for url in urls]


@pytest.fixture(autouse=True)
def _isolated_registry():
    from agent.web_search_registry import _reset_for_tests
    from tools.web_tools import _reset_search_circuit_for_tests

    _reset_for_tests()
    _reset_search_circuit_for_tests()
    yield
    _reset_for_tests()
    _reset_search_circuit_for_tests()


@pytest.mark.parametrize(
    "primary_response",
    [
        {"success": False, "error": "primary unavailable"},
        {
            "success": True,
            "data": {
                "web": [],
                "unresponsive_engines": [
                    ["brave", "Too Many Requests"],
                    ["google", "CAPTCHA"],
                ],
            },
        },
    ],
    ids=["error", "successful-empty"],
)
def test_search_falls_back_on_error_and_successful_empty(
    monkeypatch, primary_response
):
    from agent.web_search_registry import register_provider
    from tools import web_tools

    calls: list[str] = []
    register_provider(_SearchProvider("ddgs", primary_response, calls))
    register_provider(
        _SearchProvider(
            "searxng",
            {
                "success": True,
                "data": {
                    "web": [
                        {
                            "title": "Fallback result",
                            "url": "https://example.org/result",
                            "description": "usable",
                            "position": 0,
                        }
                    ]
                },
            },
            calls,
        )
    )
    monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)
    monkeypatch.setattr(web_tools, "_get_search_backend", lambda: "ddgs")

    result = json.loads(web_tools.web_search_tool("fallback me", limit=3))

    assert calls == ["ddgs", "searxng"]
    assert result["success"] is True
    assert result["data"]["provider"] == "searxng"
    assert result["data"]["fallback"] is True
    assert result["data"]["provenance"][0]["provider"] == "ddgs"
    assert result["data"]["provenance"][0]["status"] in {
        "error",
        "empty",
        "degraded",
    }
    if result["data"]["provenance"][0]["status"] in {"empty", "degraded"}:
        assert result["data"]["provenance"][0]["details"] == {
            "unresponsive_engines": [
                ["brave", "Too Many Requests"],
                ["google", "CAPTCHA"],
            ]
        }
    assert result["data"]["provenance"][1]["status"] == "success"


def test_repeated_provider_failures_open_circuit_and_skip_until_cooldown(
    monkeypatch,
):
    from tools import web_tools

    calls: list[str] = []
    failing = _SearchProvider(
        "ddgs", {"success": False, "error": "No results found"}, calls
    )
    backup = _SearchProvider(
        "searxng",
        {
            "success": True,
            "data": {
                "web": [
                    {
                        "title": "Fallback result",
                        "url": "https://example.org/result",
                        "description": "usable",
                        "position": 0,
                    }
                ]
            },
        },
        calls,
    )

    now = [100.0]
    monkeypatch.setattr(web_tools.time, "monotonic", lambda: now[0])
    for _ in range(web_tools._SEARCH_FAILURE_THRESHOLD):
        result = web_tools._search_with_fallback(
            [failing, backup], "bounded fallback", 3
        )
        assert result["success"] is True

    calls.clear()
    result = web_tools._search_with_fallback(
        [failing, backup], "bounded fallback", 3
    )
    assert calls == ["searxng"]
    assert result["data"]["provenance"][0] == {
        "provider": "ddgs",
        "status": "circuit_open",
        "error": "provider temporarily skipped after repeated failures",
    }

    now[0] += web_tools._SEARCH_COOLDOWN_SECONDS + 1
    calls.clear()
    failing._response = {
        "success": True,
        "data": {
            "web": [
                {
                    "title": "Recovered primary",
                    "url": "https://example.org/recovered",
                    "description": "usable",
                    "position": 0,
                }
            ]
        },
    }
    result = web_tools._search_with_fallback(
        [failing, backup], "bounded fallback", 3
    )
    assert calls == ["ddgs"]
    assert result["data"]["provider"] == "ddgs"


@pytest.mark.parametrize(
    "junk",
    [
        [{}],
        [
            {
                "title": "Checking your browser",
                "url": "https://example.org/challenge",
                "description": "Complete the CAPTCHA to continue",
            }
        ],
    ],
    ids=["empty-object", "captcha-result"],
)
def test_search_falls_back_when_nonempty_results_are_unusable(junk):
    from tools import web_tools

    calls: list[str] = []
    primary = _SearchProvider(
        "ddgs", {"success": True, "data": {"web": junk}}, calls
    )
    backup = _SearchProvider(
        "searxng",
        {
            "success": True,
            "data": {
                "web": [
                    {
                        "title": "Usable result",
                        "url": "https://example.org/result",
                        "description": "Concrete source metadata",
                    }
                ]
            },
        },
        calls,
    )

    result = web_tools._search_with_fallback(
        [primary, backup], "quality fallback", 3
    )

    assert calls == ["ddgs", "searxng"]
    assert result["success"] is True
    assert result["data"]["provider"] == "searxng"
    assert result["data"]["provenance"][0]["status"] == "degraded"


def test_degraded_empty_response_opens_search_circuit(monkeypatch):
    from tools import web_tools

    calls: list[str] = []
    degraded = _SearchProvider(
        "searxng",
        {
            "success": True,
            "data": {
                "web": [],
                "unresponsive_engines": [["google", "CAPTCHA"]],
            },
        },
        calls,
    )
    backup = _SearchProvider(
        "ddgs",
        {
            "success": True,
            "data": {
                "web": [
                    {
                        "title": "Fallback",
                        "url": "https://example.org/fallback",
                        "description": "usable",
                    }
                ]
            },
        },
        calls,
    )
    monkeypatch.setattr(web_tools.time, "monotonic", lambda: 100.0)

    for _ in range(web_tools._SEARCH_FAILURE_THRESHOLD):
        assert web_tools._search_with_fallback(
            [degraded, backup], "degraded engines", 3
        )["success"]

    calls.clear()
    result = web_tools._search_with_fallback(
        [degraded, backup], "degraded engines", 3
    )

    assert calls == ["ddgs"]
    assert result["data"]["provenance"][0]["status"] == "circuit_open"


def test_clean_empty_response_does_not_open_search_circuit(monkeypatch):
    from tools import web_tools

    calls: list[str] = []
    clean_empty = _SearchProvider(
        "searxng", {"success": True, "data": {"web": []}}, calls
    )
    backup = _SearchProvider(
        "ddgs",
        {
            "success": True,
            "data": {
                "web": [
                    {
                        "title": "Fallback",
                        "url": "https://example.org/fallback",
                        "description": "usable",
                    }
                ]
            },
        },
        calls,
    )
    monkeypatch.setattr(web_tools.time, "monotonic", lambda: 100.0)

    for _ in range(web_tools._SEARCH_FAILURE_THRESHOLD + 1):
        assert web_tools._search_with_fallback(
            [clean_empty, backup], "clean empty", 3
        )["success"]

    assert calls.count("searxng") == web_tools._SEARCH_FAILURE_THRESHOLD + 1


def test_search_half_open_allows_only_one_probe(monkeypatch):
    from tools import web_tools

    now = [100.0]
    monkeypatch.setattr(web_tools.time, "monotonic", lambda: now[0])
    for _ in range(web_tools._SEARCH_FAILURE_THRESHOLD):
        web_tools._record_search_provider_failure("searxng")

    now[0] += web_tools._SEARCH_COOLDOWN_SECONDS + 1
    assert web_tools._search_circuit_open("searxng") is False
    assert web_tools._search_circuit_open("searxng") is True

    web_tools._record_search_provider_operational("searxng")
    assert web_tools._search_circuit_open("searxng") is False


def test_extract_retries_only_unusable_urls_and_preserves_order(monkeypatch):
    from agent.web_search_registry import register_provider
    from tools import web_tools

    url_a = "https://example.org/article-a"
    url_b = "https://example.org/article-b"
    article_a = " ".join(["primary article evidence"] * 30)
    article_b = " ".join(["rendered fallback evidence"] * 30)
    calls: list[tuple[str, List[str]]] = []

    register_provider(
        _ExtractProvider(
            "jina",
            {
                url_a: {
                    "title": "Article A",
                    "content": article_a,
                    "raw_content": article_a,
                },
                url_b: {
                    "title": "Checking your browser",
                    "content": "Just a moment... Verify you are human to continue.",
                    "raw_content": "Just a moment... Verify you are human to continue.",
                },
            },
            calls,
        )
    )
    register_provider(
        _ExtractProvider(
            "local_browser",
            {
                url_b: {
                    "title": "Article B",
                    "content": article_b,
                    "raw_content": article_b,
                }
            },
            calls,
        )
    )
    monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)
    monkeypatch.setattr(web_tools, "_get_extract_backend", lambda: "jina")

    async def _safe(_url: str) -> bool:
        return True

    monkeypatch.setattr(web_tools, "async_is_safe_url", _safe)

    result = json.loads(asyncio.run(web_tools.web_extract_tool([url_a, url_b])))

    assert calls == [
        ("jina", [url_a, url_b]),
        ("local_browser", [url_b]),
    ]
    assert [item["url"] for item in result["results"]] == [url_a, url_b]
    assert result["results"][0]["provider"] == "jina"
    assert result["results"][0]["fallback"] is False
    assert result["results"][0]["content_class"] == "article"
    assert result["results"][1]["provider"] == "local_browser"
    assert result["results"][1]["fallback"] is True
    assert result["results"][1]["content_class"] == "article"
    assert [
        attempt["content_class"]
        for attempt in result["results"][1]["provenance"]
    ] == ["challenge", "article"]


def test_extract_matches_reordered_provider_results_by_url():
    from tools import web_tools

    url_a = "https://example.org/article-a"
    url_b = "https://example.org/article-b"
    article_a = " ".join(["article a evidence"] * 30)
    article_b = " ".join(["article b evidence"] * 30)

    class ReorderedProvider(_ExtractProvider):
        async def extract(self, urls, **kwargs):
            self._calls.append((self.name, list(urls)))
            return [
                dict(self._responses[url], url=url)
                for url in reversed(urls)
            ]

    provider = ReorderedProvider(
        "jina",
        {
            url_a: {"title": "A", "content": article_a},
            url_b: {"title": "B", "content": article_b},
        },
        [],
    )

    results = asyncio.run(
        web_tools._extract_with_fallback(
            [provider], [url_a, url_b], format="markdown"
        )
    )

    assert [result["url"] for result in results] == [url_a, url_b]
    assert [result["title"] for result in results] == ["A", "B"]


def test_extract_missing_provider_result_cannot_bind_to_wrong_url():
    from tools import web_tools

    url_a = "https://example.org/article-a"
    url_b = "https://example.org/article-b"
    article_b = " ".join(["article b evidence"] * 30)

    class MissingProvider(_ExtractProvider):
        async def extract(self, urls, **kwargs):
            self._calls.append((self.name, list(urls)))
            return [dict(self._responses[url_b], url=url_b)]

    provider = MissingProvider(
        "jina", {url_b: {"title": "B", "content": article_b}}, []
    )

    results = asyncio.run(
        web_tools._extract_with_fallback(
            [provider], [url_a, url_b], format="markdown"
        )
    )

    assert results[0]["url"] == url_a
    assert results[0]["status"] == "error"
    assert "no result" in results[0]["error"].lower()
    assert results[1]["url"] == url_b
    assert results[1]["title"] == "B"


def test_extract_capability_backend_is_resolved_after_plugin_discovery(monkeypatch):
    from agent.web_search_registry import register_provider
    from tools import web_tools

    url = "https://example.org/article"
    calls: list[tuple[str, List[str]]] = []
    article = " ".join(["configured Jina article"] * 30)

    def discover_plugins():
        register_provider(
            _SearchProvider(
                "ddgs", {"success": True, "data": {"web": []}}, []
            )
        )
        register_provider(
            _ExtractProvider(
                "jina",
                {
                    url: {
                        "title": "Article",
                        "content": article,
                        "raw_content": article,
                    }
                },
                calls,
            )
        )

    monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", discover_plugins)
    monkeypatch.setattr(
        web_tools,
        "_load_web_config",
        lambda: {
            "backend": "ddgs",
            "search_backend": "ddgs",
            "extract_backend": "jina",
        },
    )

    async def _safe(_url: str) -> bool:
        return True

    monkeypatch.setattr(web_tools, "async_is_safe_url", _safe)

    result = json.loads(asyncio.run(web_tools.web_extract_tool([url])))

    assert result["results"][0]["provider"] == "jina"
    assert calls == [("jina", [url])]


def test_fxtwitter_json_is_extracted_directly_before_jina(monkeypatch):
    from agent.web_search_registry import register_provider
    from tools import web_tools

    url = "https://api.fxtwitter.com/status/123456789"
    calls: list[tuple[str, List[str]]] = []
    register_provider(
        _ExtractProvider(
            "jina",
            {
                url: {
                    "title": "should not run",
                    "content": "Jina should not receive a known JSON endpoint",
                    "raw_content": "Jina should not receive a known JSON endpoint",
                }
            },
            calls,
        )
    )
    monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)
    monkeypatch.setattr(web_tools, "_get_extract_backend", lambda: "jina")

    async def _safe(_url: str) -> bool:
        return True

    async def _direct(_url: str):
        content = json.dumps(
            {
                "tweet": {
                    "id": "123456789",
                    "text": "A directly fetched public post",
                    "author": {"name": "Example"},
                }
            }
        )
        return {
            "url": _url,
            "title": "FXTwitter status 123456789",
            "content": content,
            "raw_content": content,
            "metadata": {"provider": "direct_json", "validated": True},
        }

    monkeypatch.setattr(web_tools, "async_is_safe_url", _safe)
    monkeypatch.setattr(web_tools, "_extract_known_json_url", _direct)

    result = json.loads(asyncio.run(web_tools.web_extract_tool([url])))

    assert calls == []
    assert result["results"][0]["provider"] == "direct_json"
    assert result["results"][0]["content_class"] == "article"
    assert result["results"][0]["provenance"][0]["provider"] == "direct_json"


def test_fxtwitter_error_json_is_not_promoted_to_article(monkeypatch):
    from tools import web_tools

    url = "https://api.fxtwitter.com/status/123456789"

    async def invalid_direct(_url):
        content = json.dumps({"error": "Tweet not found"})
        return {
            "url": _url,
            "title": "",
            "content": content,
            "raw_content": content,
            "error": "Direct FXTwitter JSON payload is invalid",
        }

    monkeypatch.setattr(web_tools, "_extract_known_json_url", invalid_direct)

    results = asyncio.run(
        web_tools._extract_with_fallback([], [url], format="markdown")
    )

    assert results[0]["content_class"] != "article"
    assert results[0]["status"] != "success"
    assert results[0]["error"] == "Direct FXTwitter JSON payload is invalid"


def test_extract_backend_auto_selection_is_capability_aware(monkeypatch):
    from agent.web_search_registry import register_provider
    from tools import web_tools

    register_provider(
        _SearchProvider("ddgs", {"success": True, "data": {"web": []}}, [])
    )
    register_provider(_ExtractProvider("jina", {}, []))
    monkeypatch.setattr(web_tools, "_load_web_config", lambda: {})
    monkeypatch.setattr(web_tools, "_get_backend", lambda: "ddgs")

    assert web_tools._get_capability_backend("extract") == "jina"


def test_configured_extract_backend_remains_primary_during_cooldown(monkeypatch):
    from agent.web_search_registry import register_provider
    from tools import web_tools

    jina = _ExtractProvider("jina", {}, [])
    jina.is_available = lambda: False
    register_provider(jina)
    register_provider(
        _SearchProvider("ddgs", {"success": True, "data": {"web": []}}, [])
    )
    monkeypatch.setattr(
        web_tools,
        "_load_web_config",
        lambda: {"backend": "ddgs", "extract_backend": "jina"},
    )

    assert web_tools._get_capability_backend("extract") == "jina"


def test_configured_jina_cooldown_still_reaches_local_fallback_twice(monkeypatch):
    from agent.web_search_registry import register_provider
    from tools import web_tools

    url = "https://example.org/article"
    article = " ".join(["local fallback evidence"] * 30)
    calls: list[tuple[str, List[str]]] = []
    jina = _ExtractProvider(
        "jina",
        {
            url: {
                "title": "",
                "content": "",
                "raw_content": "",
                "error": "Jina Reader is in cooldown for 300s after HTTP 401",
            }
        },
        calls,
    )
    jina.is_available = lambda: False
    local = _ExtractProvider(
        "local_browser",
        {url: {"title": "Article", "content": article, "raw_content": article}},
        calls,
    )
    register_provider(jina)
    register_provider(local)
    monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)
    monkeypatch.setattr(
        web_tools,
        "_load_web_config",
        lambda: {"backend": "ddgs", "extract_backend": "jina"},
    )

    async def safe(_url):
        return True

    monkeypatch.setattr(web_tools, "async_is_safe_url", safe)

    first = json.loads(asyncio.run(web_tools.web_extract_tool([url])))
    second = json.loads(asyncio.run(web_tools.web_extract_tool([url])))

    assert first["results"][0]["provider"] == "local_browser"
    assert second["results"][0]["provider"] == "local_browser"
    assert calls == [
        ("jina", [url]),
        ("local_browser", [url]),
        ("jina", [url]),
        ("local_browser", [url]),
    ]


def test_fxtwitter_direct_extract_honors_website_policy(monkeypatch):
    from agent.web_search_registry import register_provider
    from tools import web_tools

    url = "https://api.fxtwitter.com/status/123456789"
    calls: list[tuple[str, List[str]]] = []
    register_provider(_ExtractProvider("jina", {}, calls))
    monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)
    monkeypatch.setattr(web_tools, "_get_extract_backend", lambda: "jina")

    async def _safe(_url: str) -> bool:
        return True

    monkeypatch.setattr(web_tools, "async_is_safe_url", _safe)
    monkeypatch.setattr(
        "tools.website_policy.check_website_access",
        lambda _url: {
            "host": "api.fxtwitter.com",
            "rule": "api.fxtwitter.com",
            "source": "test-policy",
            "message": "Blocked by website policy",
        },
    )

    result = json.loads(asyncio.run(web_tools.web_extract_tool([url])))

    assert calls == []
    assert result["results"][0]["error"] == "Blocked by website policy"
    assert result["results"][0]["blocked_by_policy"]["rule"] == "api.fxtwitter.com"
    assert result["results"][0]["provenance"] == [
        {
            "provider": "direct_json",
            "status": "error",
            "content_class": "empty",
            "completeness": 0.0,
            "blocker": "Blocked by website policy",
            "error": "Blocked by website policy",
        }
    ]


def test_policy_block_is_terminal_and_never_falls_back(monkeypatch):
    from agent.web_search_registry import register_provider
    from tools import web_tools

    url = "https://blocked.example/article"
    calls: list[tuple[str, List[str]]] = []
    register_provider(
        _ExtractProvider(
            "firecrawl",
            {
                url: {
                    "title": "",
                    "content": "",
                    "raw_content": "",
                    "error": "Blocked by website policy",
                    "blocked_by_policy": {
                        "host": "blocked.example",
                        "rule": "blocked.example",
                        "source": "test-policy",
                    },
                }
            },
            calls,
        )
    )
    register_provider(
        _ExtractProvider(
            "local_browser",
            {
                url: {
                    "title": "Forbidden fetch",
                    "content": " ".join(["must not be fetched"] * 30),
                    "raw_content": " ".join(["must not be fetched"] * 30),
                }
            },
            calls,
        )
    )
    monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)
    monkeypatch.setattr(web_tools, "_get_extract_backend", lambda: "firecrawl")

    async def _safe(_url: str) -> bool:
        return True

    monkeypatch.setattr(web_tools, "async_is_safe_url", _safe)

    result = json.loads(asyncio.run(web_tools.web_extract_tool([url])))

    assert calls == [("firecrawl", [url])]
    assert result["results"][0]["error"] == "Blocked by website policy"
    assert result["results"][0]["provider"] == "firecrawl"
    assert result["results"][0]["fallback_attempted"] is False
