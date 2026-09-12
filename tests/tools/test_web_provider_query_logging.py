"""Regression tests: routine web-search telemetry must not retain queries."""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from unittest.mock import patch


def _assert_fingerprinted(caplog, query: str, expected_prefix: str) -> None:
    assert query not in caplog.text
    assert expected_prefix in caplog.text


def test_exa_routine_log_fingerprints_query(monkeypatch, caplog):
    from plugins.web.exa import provider

    client = SimpleNamespace(search=lambda *_args, **_kwargs: SimpleNamespace(results=[]))
    monkeypatch.setattr(provider, "_get_exa_client", lambda: client)
    monkeypatch.setattr(provider, "use_keyless", lambda *_args: False)
    query = "sensitive exa marker"

    with caplog.at_level(logging.INFO, logger=provider.logger.name):
        provider.ExaWebSearchProvider().search(query, limit=3)

    _assert_fingerprinted(caplog, query, "04ea051e0bc6")


def test_tavily_routine_log_fingerprints_query(monkeypatch, caplog):
    from plugins.web.tavily import provider

    monkeypatch.setattr(provider, "_tavily_request", lambda *_args, **_kwargs: {"results": []})
    monkeypatch.setattr(provider, "_auth", lambda _action: ("test-key", None, ""))
    query = "sensitive tavily marker"

    with caplog.at_level(logging.INFO, logger=provider.logger.name):
        provider.TavilyWebSearchProvider().search(query, limit=3)

    _assert_fingerprinted(caplog, query, "2b0d6d2300e2")


def test_firecrawl_routine_log_fingerprints_query(monkeypatch, caplog):
    from plugins.web.firecrawl import provider

    client = SimpleNamespace(search=lambda **_kwargs: {"web": []})
    monkeypatch.setattr(provider, "_get_firecrawl_client", lambda: client)
    monkeypatch.setattr(provider, "_use_keyless_ring", lambda: False)
    query = "sensitive firecrawl marker"

    with caplog.at_level(logging.INFO, logger=provider.logger.name):
        provider.FirecrawlWebSearchProvider().search(query, limit=3)

    _assert_fingerprinted(caplog, query, "11280536570b")


def test_parallel_routine_log_fingerprints_query(monkeypatch, caplog):
    from plugins.web.parallel import provider

    client = SimpleNamespace(search=lambda **_kwargs: SimpleNamespace(results=[]))
    monkeypatch.setattr(provider, "_get_sync_client", lambda: client)
    monkeypatch.setattr(provider, "_get_parallel_api_key", lambda: "test-key")
    monkeypatch.setattr(provider, "use_keyless", lambda *_args: False)
    query = "sensitive parallel marker"

    with caplog.at_level(logging.INFO, logger=provider.logger.name):
        provider.ParallelWebSearchProvider().search(query, limit=3)

    _assert_fingerprinted(caplog, query, "f07d469a51b5")


def test_dispatcher_log_and_debug_payload_do_not_retain_plaintext_query(
    monkeypatch, caplog
):
    from agent.web_search_provider import WebSearchProvider
    from agent.web_search_registry import _reset_for_tests, register_provider
    from tools import web_tools

    class FakeProvider(WebSearchProvider):
        @property
        def name(self):
            return "test-search"

        def is_available(self):
            return True

        def supports_search(self):
            return True

        def search(self, query, limit=5):
            return {
                "success": True,
                "data": {
                    "web": [
                        {
                            "title": "Result",
                            "url": "https://example.org/result",
                            "description": "Concrete source metadata",
                        }
                    ]
                },
            }

    query = "sensitive dispatcher marker"
    _reset_for_tests()
    register_provider(FakeProvider())
    monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)
    monkeypatch.setattr(web_tools, "_get_search_backend", lambda: "test-search")
    monkeypatch.setattr("tools.interrupt.is_interrupted", lambda: False)

    try:
        with (
            caplog.at_level(logging.INFO, logger=web_tools.logger.name),
            patch.object(web_tools._debug, "log_call") as log_call,
            patch.object(web_tools._debug, "save"),
        ):
            web_tools.web_search_tool(query, limit=5)
    finally:
        _reset_for_tests()

    _assert_fingerprinted(caplog, query, "5900e17ab9b7")
    debug_payload = log_call.call_args.args[1]
    assert "query" not in debug_payload["parameters"]
    assert debug_payload["parameters"]["query_sha256"] == (
        "5900e17ab9b74980b5f1709e0a3cb9e9c1e36fe129ea236a9f063eccd1588198"
    )


def test_dispatcher_provider_exception_does_not_retain_query(monkeypatch):
    """A provider exception must not flow into the returned or persisted error."""
    from agent.web_search_provider import WebSearchProvider
    from agent.web_search_registry import _reset_for_tests, register_provider
    from tools import web_tools

    class FailingProvider(WebSearchProvider):
        @property
        def name(self):
            return "echo-failure"

        def is_available(self):
            return True

        def supports_search(self):
            return True

        def search(self, query, limit=5):
            raise RuntimeError(f"provider rejected query {query}")

    query = "PRIVATE_DEBUG_MARKER_a82c1"
    _reset_for_tests()
    register_provider(FailingProvider())
    monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)
    monkeypatch.setattr(web_tools, "_get_search_backend", lambda: "echo-failure")
    monkeypatch.setattr(
        web_tools,
        "_load_web_config",
        lambda: {"backend": "echo-failure", "keyless_rescue": False},
    )
    monkeypatch.setattr("tools.interrupt.is_interrupted", lambda: False)

    try:
        with (
            patch.object(web_tools._debug, "log_call") as log_call,
            patch.object(web_tools._debug, "save"),
        ):
            result_text = web_tools.web_search_tool(query, limit=5)
    finally:
        _reset_for_tests()

    debug_payload = log_call.call_args.args[1]
    assert query not in result_text
    assert query not in json.dumps(debug_payload)
    assert "echo-failure" in debug_payload["error"]
    assert "6df054cb8a19" in debug_payload["error"]
