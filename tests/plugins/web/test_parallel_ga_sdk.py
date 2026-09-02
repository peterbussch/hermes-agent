"""Parallel GA SDK request and compatibility contracts.

The real pinned SDK supplies client specs and response models.  Client methods
are replaced at the transport boundary so these tests cannot issue requests.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import AsyncMock, Mock

import pytest
import parallel as parallel_sdk
from parallel import AsyncParallel, Parallel
from parallel.types import (
    ExtractError,
    ExtractResponse,
    ExtractResult,
    SearchResult,
    WebSearchResult,
)

from agent import web_search_provider
from agent import secret_scope
from hermes_cli.config import DEFAULT_CONFIG
from plugins.web.parallel import provider
from plugins.web.parallel.provider import (
    ParallelWebSearchProvider,
    _resolve_search_mode,
)
from tools import web_tools


@contextmanager
def _secret_scope(secrets):
    token = secret_scope.set_secret_scope(secrets)
    try:
        yield
    finally:
        secret_scope.reset_secret_scope(token)


@pytest.fixture
def isolated_parallel_client_cache(monkeypatch):
    """Use real profile scopes with request-free recording SDK clients."""
    original_multiplex = secret_scope.is_multiplex_active()
    outer_scope = secret_scope.set_secret_scope(None)

    class RecordingParallelClient:
        instances = []

        def __init__(self, api_key):
            self.api_key = api_key
            self.instances.append(self)

    monkeypatch.setattr(parallel_sdk, "Parallel", RecordingParallelClient)
    monkeypatch.setattr(parallel_sdk, "AsyncParallel", RecordingParallelClient)
    monkeypatch.setattr(provider, "_ensure_parallel_sdk_installed", lambda: None)
    monkeypatch.setenv("PARALLEL_API_KEY", "process-key-must-not-leak")
    secret_scope.set_multiplex_active(True)
    provider._reset_clients_for_tests()

    yield RecordingParallelClient

    provider._reset_clients_for_tests()
    secret_scope.reset_secret_scope(outer_scope)
    secret_scope.set_multiplex_active(original_multiplex)


_CLIENT_CACHE_VARIANTS = [
    ("_get_sync_client", "_parallel_client", "_parallel_client_credential_id"),
    (
        "_get_async_client",
        "_async_parallel_client",
        "_async_parallel_client_credential_id",
    ),
]


@pytest.mark.parametrize(
    ("getter_name", "client_slot", "identity_slot"), _CLIENT_CACHE_VARIANTS
)
def test_profile_switch_replaces_cached_client(
    isolated_parallel_client_cache,
    getter_name,
    client_slot,
    identity_slot,
):
    """Catch Profile B receiving Profile A's cached credential/client."""
    getter = getattr(provider, getter_name)

    with _secret_scope({"PARALLEL_API_KEY": "profile-a-key"}):
        client_a = getter()
    with _secret_scope({"PARALLEL_API_KEY": "profile-b-key"}):
        client_b = getter()

    assert client_a is not client_b
    assert client_a.api_key == "profile-a-key"
    assert client_b.api_key == "profile-b-key"
    assert getattr(web_tools, client_slot) is client_b
    assert getattr(web_tools, identity_slot) not in {
        "profile-b-key",
        b"profile-b-key",
    }


@pytest.mark.parametrize(
    ("getter_name", "client_slot", "identity_slot"), _CLIENT_CACHE_VARIANTS
)
def test_empty_profile_scope_never_reuses_cached_client(
    isolated_parallel_client_cache,
    getter_name,
    client_slot,
    identity_slot,
):
    """Catch a keyless profile borrowing either cached or process-env credentials."""
    getter = getattr(provider, getter_name)

    with _secret_scope({"PARALLEL_API_KEY": "profile-a-key"}):
        client_a = getter()
    with _secret_scope({}):
        with pytest.raises(ValueError, match="PARALLEL_API_KEY"):
            getter()

    assert client_a.api_key == "profile-a-key"
    assert getattr(web_tools, client_slot) is client_a
    assert getattr(web_tools, identity_slot) is not None


@pytest.mark.parametrize(
    ("getter_name", "client_slot", "identity_slot"), _CLIENT_CACHE_VARIANTS
)
def test_same_profile_credential_reuses_cached_client(
    isolated_parallel_client_cache,
    getter_name,
    client_slot,
    identity_slot,
):
    """Catch safe same-credential singleton reuse being discarded."""
    getter = getattr(provider, getter_name)

    with _secret_scope({"PARALLEL_API_KEY": "stable-profile-key"}):
        first = getter()
        second = getter()

    assert first is second
    assert getattr(web_tools, client_slot) is first
    assert getattr(web_tools, identity_slot) is not None


@pytest.mark.parametrize(
    ("getter_name", "client_slot", "identity_slot"), _CLIENT_CACHE_VARIANTS
)
def test_rotated_profile_credential_replaces_cached_client(
    isolated_parallel_client_cache,
    getter_name,
    client_slot,
    identity_slot,
):
    """Catch an in-process credential rotation retaining the stale SDK client."""
    getter = getattr(provider, getter_name)

    with _secret_scope({"PARALLEL_API_KEY": "pre-rotation-key"}):
        before = getter()
    with _secret_scope({"PARALLEL_API_KEY": "post-rotation-key"}):
        after = getter()

    assert before is not after
    assert before.api_key == "pre-rotation-key"
    assert after.api_key == "post-rotation-key"
    assert getattr(web_tools, client_slot) is after
    assert getattr(web_tools, identity_slot) is not None


def test_reset_clears_clients_and_credential_identities(isolated_parallel_client_cache):
    """Catch the test/runtime reset leaving credential identity state behind."""
    with _secret_scope({"PARALLEL_API_KEY": "profile-a-key"}):
        provider._get_sync_client()
        provider._get_async_client()

    provider._reset_clients_for_tests()

    assert web_tools._parallel_client is None
    assert web_tools._async_parallel_client is None
    assert web_tools._parallel_client_credential_id is None
    assert web_tools._async_parallel_client_credential_id is None


def test_availability_honors_authoritative_empty_profile_scope(
    isolated_parallel_client_cache,
):
    """Catch provider discovery borrowing another profile's process-env key."""
    parallel_provider = ParallelWebSearchProvider()

    with _secret_scope({"PARALLEL_API_KEY": "profile-a-key"}):
        assert parallel_provider.is_available() is True
    with _secret_scope({}):
        assert parallel_provider.is_available() is False


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


def test_scoped_mode_miss_ignores_conflicting_process_mode(
    isolated_parallel_client_cache,
    monkeypatch,
):
    """Catch a scoped mode miss borrowing another profile's process value."""
    client = Mock(spec=Parallel)
    client.search.return_value = SearchResult(
        results=[],
        search_id="search-test",
        session_id="session-test",
        usage=None,
        warnings=None,
    )
    monkeypatch.setattr(provider, "_get_sync_client", lambda: client)
    monkeypatch.setattr(web_tools, "_load_web_config", lambda: {})
    monkeypatch.setenv("PARALLEL_SEARCH_MODE", "fast")
    monkeypatch.setattr("tools.interrupt.is_interrupted", lambda: False)

    with _secret_scope({"PARALLEL_API_KEY": "profile-key-without-mode"}):
        result = ParallelWebSearchProvider().search("scoped default query")

    assert result == {"success": True, "data": {"web": []}}
    assert client.search.call_args.kwargs["mode"] == "advanced"


def test_scoped_legacy_mode_wins_over_conflicting_process_mode(
    isolated_parallel_client_cache,
    monkeypatch,
):
    """Catch a scoped legacy mode being ignored for the process value."""
    client = Mock(spec=Parallel)
    client.search.return_value = SearchResult(
        results=[],
        search_id="search-test",
        session_id="session-test",
        usage=None,
        warnings=None,
    )
    monkeypatch.setattr(provider, "_get_sync_client", lambda: client)
    monkeypatch.setattr(web_tools, "_load_web_config", lambda: {})
    monkeypatch.setenv("PARALLEL_SEARCH_MODE", "agentic")
    monkeypatch.setattr("tools.interrupt.is_interrupted", lambda: False)

    with _secret_scope({
        "PARALLEL_API_KEY": "profile-key",
        "PARALLEL_SEARCH_MODE": "fast",
    }):
        result = ParallelWebSearchProvider().search("scoped mode query")

    assert result == {"success": True, "data": {"web": []}}
    assert client.search.call_args.kwargs["mode"] == "basic"


def test_unscoped_process_legacy_mode_remains_compatible(monkeypatch):
    """Catch ordinary single-profile process-mode compatibility regressing."""
    client = Mock(spec=Parallel)
    client.search.return_value = SearchResult(
        results=[],
        search_id="search-test",
        session_id="session-test",
        usage=None,
        warnings=None,
    )
    monkeypatch.setattr(provider, "_get_sync_client", lambda: client)
    monkeypatch.setattr(web_tools, "_load_web_config", lambda: {})
    monkeypatch.setenv("PARALLEL_SEARCH_MODE", "fast")
    monkeypatch.setattr("tools.interrupt.is_interrupted", lambda: False)
    original_multiplex = secret_scope.is_multiplex_active()
    outer_scope = secret_scope.set_secret_scope(None)
    try:
        secret_scope.set_multiplex_active(False)
        result = ParallelWebSearchProvider().search("unscoped mode query")
    finally:
        secret_scope.reset_secret_scope(outer_scope)
        secret_scope.set_multiplex_active(original_multiplex)

    assert result == {"success": True, "data": {"web": []}}
    assert client.search.call_args.kwargs["mode"] == "basic"


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
