"""Parallel GA SDK request and compatibility contracts.

The real pinned SDK supplies client specs and response models.  Client methods
are replaced at the transport boundary so these tests cannot issue requests.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest
from parallel import AsyncParallel, Parallel
from parallel.types import (
    ExtractError,
    ExtractResponse,
    ExtractResult,
    SearchResult,
    WebSearchResult,
)

from agent import web_search_provider
from hermes_cli.config import DEFAULT_CONFIG
from plugins.web.parallel import provider
from plugins.web.parallel.provider import (
    ParallelWebSearchProvider,
    _resolve_search_mode,
)
from tools import web_tools


@pytest.mark.parametrize(
    ("configured", "legacy", "expected"),
    [
        ("turbo", None, "turbo"),
        ("fast", None, "fast"),
        ("basic", None, "basic"),
        ("advanced", None, "advanced"),
        ("agentic", None, "advanced"),
        ("one-shot", None, "basic"),
        (None, "fast", "basic"),
        (None, "agentic", "advanced"),
        (None, "one-shot", "basic"),
        (None, None, "advanced"),
        ("not-a-mode", "fast", "advanced"),
        (None, "not-a-mode", "advanced"),
    ],
)
def test_parallel_mode_contract(configured, legacy, expected):
    """Catch GA modes or legacy beta aliases being reinterpreted."""
    assert _resolve_search_mode(configured, legacy) == expected


def test_parallel_mode_normalizes_explicit_values():
    """Catch valid explicit GA modes being rejected due to case or whitespace."""
    assert _resolve_search_mode("  FaSt  ", "agentic") == "fast"


def test_parallel_search_mode_default_cannot_mask_legacy_profiles():
    """Catch a nonempty default silently overriding existing profile env mode."""
    assert DEFAULT_CONFIG["web"]["parallel_search_mode"] == ""


def test_search_uses_ga_client_and_advanced_settings(monkeypatch):
    """Catch a beta call or max_results placed outside advanced_settings."""
    client = Mock(spec=Parallel)
    client.search.return_value = SearchResult(
        results=[
            WebSearchResult(
                excerpts=["first excerpt", "second excerpt"],
                title="Example",
                url="https://example.com",
            )
        ],
        search_id="search-test",
        session_id="session-test",
        usage=None,
        warnings=None,
    )
    monkeypatch.setattr(provider, "_get_sync_client", lambda: client)
    monkeypatch.setattr(web_tools, "_load_web_config", lambda: {})
    monkeypatch.setattr(web_search_provider, "get_provider_env", lambda _name: "")
    monkeypatch.setattr("tools.interrupt.is_interrupted", lambda: False)

    result = ParallelWebSearchProvider().search("query", limit=7)

    assert result == {
        "success": True,
        "data": {
            "web": [
                {
                    "url": "https://example.com",
                    "title": "Example",
                    "description": "first excerpt second excerpt",
                    "position": 1,
                }
            ]
        },
    }
    client.search.assert_called_once_with(
        search_queries=["query"],
        objective="query",
        mode="advanced",
        advanced_settings={"max_results": 7},
    )


def test_search_reads_legacy_mode_through_profile_config(monkeypatch):
    """Catch direct os.environ lookup or legacy fast becoming GA fast."""
    client = Mock(spec=Parallel)
    client.search.return_value = SearchResult(
        results=[],
        search_id="search-test",
        session_id="session-test",
        usage=None,
        warnings=None,
    )
    requested_names = []

    def profile_value(name):
        requested_names.append(name)
        return "fast"

    monkeypatch.setattr(provider, "_get_sync_client", lambda: client)
    monkeypatch.setattr(
        web_tools,
        "_load_web_config",
        lambda: {"parallel_search_mode": ""},
    )
    monkeypatch.setattr(web_search_provider, "get_provider_env", profile_value)
    monkeypatch.setattr("tools.interrupt.is_interrupted", lambda: False)

    result = ParallelWebSearchProvider().search("legacy query")

    assert result == {"success": True, "data": {"web": []}}
    assert requested_names == ["PARALLEL_SEARCH_MODE"]
    assert client.search.call_args.kwargs["mode"] == "basic"


def test_search_explicit_ga_mode_wins_and_limit_remains_capped(monkeypatch):
    """Catch an explicit GA fast mode being masked or the legacy 20-result cap vanishing."""
    client = Mock(spec=Parallel)
    client.search.return_value = SearchResult(
        results=[],
        search_id="search-test",
        session_id="session-test",
        usage=None,
        warnings=None,
    )
    monkeypatch.setattr(provider, "_get_sync_client", lambda: client)
    monkeypatch.setattr(
        web_tools,
        "_load_web_config",
        lambda: {"parallel_search_mode": "fast"},
    )
    monkeypatch.setattr(
        web_search_provider,
        "get_provider_env",
        lambda _name: pytest.fail("explicit config must not consult legacy mode"),
    )
    monkeypatch.setattr("tools.interrupt.is_interrupted", lambda: False)

    result = ParallelWebSearchProvider().search("configured query", limit=99)

    assert result == {"success": True, "data": {"web": []}}
    assert client.search.call_args.kwargs == {
        "search_queries": ["configured query"],
        "objective": "configured query",
        "mode": "fast",
        "advanced_settings": {"max_results": 20},
    }


@pytest.mark.asyncio
async def test_extract_uses_ga_async_client_and_advanced_settings(monkeypatch):
    """Catch a beta call or full_content placed outside advanced_settings."""
    client = Mock(spec=AsyncParallel)
    client.extract = AsyncMock(
        return_value=ExtractResponse(
            errors=[],
            extract_id="extract-test",
            results=[],
            session_id="session-test",
            usage=None,
            warnings=None,
        )
    )
    monkeypatch.setattr(provider, "_get_async_client", lambda: client)
    monkeypatch.setattr("tools.interrupt.is_interrupted", lambda: False)

    result = await ParallelWebSearchProvider().extract(["https://example.com"])

    assert result == []
    client.extract.assert_awaited_once_with(
        urls=["https://example.com"],
        advanced_settings={"full_content": True},
    )


@pytest.mark.asyncio
async def test_extract_preserves_content_fallback_and_per_url_errors(monkeypatch):
    """Catch GA response adaptation dropping excerpts, metadata, or URL errors."""
    client = Mock(spec=AsyncParallel)
    client.extract = AsyncMock(
        return_value=ExtractResponse(
            errors=[
                ExtractError(
                    content="robots denied",
                    error_type="fetch_error",
                    http_status_code=403,
                    url="https://blocked.example",
                )
            ],
            extract_id="extract-test",
            results=[
                ExtractResult(
                    excerpts=["unused excerpt"],
                    full_content="complete page",
                    publish_date=None,
                    title="Full",
                    url="https://full.example",
                ),
                ExtractResult(
                    excerpts=["excerpt one", "excerpt two"],
                    full_content=None,
                    publish_date=None,
                    title=None,
                    url="https://excerpt.example",
                ),
            ],
            session_id="session-test",
            usage=None,
            warnings=None,
        )
    )
    monkeypatch.setattr(provider, "_get_async_client", lambda: client)
    monkeypatch.setattr("tools.interrupt.is_interrupted", lambda: False)

    result = await ParallelWebSearchProvider().extract([
        "https://full.example",
        "https://excerpt.example",
        "https://blocked.example",
    ])

    assert result == [
        {
            "url": "https://full.example",
            "title": "Full",
            "content": "complete page",
            "raw_content": "complete page",
            "metadata": {"sourceURL": "https://full.example", "title": "Full"},
        },
        {
            "url": "https://excerpt.example",
            "title": "",
            "content": "excerpt one\n\nexcerpt two",
            "raw_content": "excerpt one\n\nexcerpt two",
            "metadata": {"sourceURL": "https://excerpt.example", "title": ""},
        },
        {
            "url": "https://blocked.example",
            "title": "",
            "content": "",
            "error": "robots denied",
            "metadata": {"sourceURL": "https://blocked.example"},
        },
    ]


def test_search_preserves_interrupt_contract_without_constructing_client(monkeypatch):
    """Catch an interrupted request reaching the paid-client boundary."""
    monkeypatch.setattr("tools.interrupt.is_interrupted", lambda: True)
    monkeypatch.setattr(
        provider,
        "_get_sync_client",
        lambda: pytest.fail("interrupted search must not construct a client"),
    )

    result = ParallelWebSearchProvider().search("query")

    assert result == {"success": False, "error": "Interrupted"}


@pytest.mark.asyncio
async def test_extract_preserves_interrupt_contract_without_constructing_client(
    monkeypatch,
):
    """Catch an interrupted extraction reaching the paid-client boundary."""
    monkeypatch.setattr("tools.interrupt.is_interrupted", lambda: True)
    monkeypatch.setattr(
        provider,
        "_get_async_client",
        lambda: pytest.fail("interrupted extract must not construct a client"),
    )

    result = await ParallelWebSearchProvider().extract([
        "https://one.example",
        "https://two.example",
    ])

    assert result == [
        {"url": "https://one.example", "error": "Interrupted", "title": ""},
        {"url": "https://two.example", "error": "Interrupted", "title": ""},
    ]


def test_search_preserves_provider_failure_envelope(monkeypatch):
    """Catch SDK exceptions escaping or losing the Parallel search error class."""
    client = Mock(spec=Parallel)
    client.search.side_effect = RuntimeError("transport unavailable")
    monkeypatch.setattr(provider, "_get_sync_client", lambda: client)
    monkeypatch.setattr(web_tools, "_load_web_config", lambda: {})
    monkeypatch.setattr(web_search_provider, "get_provider_env", lambda _name: "")
    monkeypatch.setattr("tools.interrupt.is_interrupted", lambda: False)

    result = ParallelWebSearchProvider().search("query")

    assert result == {
        "success": False,
        "error": "Parallel search failed: transport unavailable",
    }


@pytest.mark.asyncio
async def test_extract_preserves_per_url_provider_failure_envelope(monkeypatch):
    """Catch SDK exceptions escaping or dropping requested URLs from failures."""
    client = Mock(spec=AsyncParallel)
    client.extract = AsyncMock(side_effect=RuntimeError("transport unavailable"))
    monkeypatch.setattr(provider, "_get_async_client", lambda: client)
    monkeypatch.setattr("tools.interrupt.is_interrupted", lambda: False)

    result = await ParallelWebSearchProvider().extract([
        "https://one.example",
        "https://two.example",
    ])

    assert result == [
        {
            "url": "https://one.example",
            "title": "",
            "content": "",
            "error": "Parallel extract failed: transport unavailable",
        },
        {
            "url": "https://two.example",
            "title": "",
            "content": "",
            "error": "Parallel extract failed: transport unavailable",
        },
    ]
