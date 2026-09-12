"""Parallel.ai web search and extraction via the GA SDK.

The paid route uses ``parallel-web>=1.3.3,<2``. Anonymous keyless fallback
remains available through Hermes' shared web-provider ring.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import secrets
import threading
from typing import Any, Dict, List

from agent.web_search_provider import query_fingerprint
from plugins.web._common import (
    SEARCH_LIMIT_CAP,
    BaseWebSearchProvider,
    document,
    keyless_extract,
    keyless_search,
    keyless_variant_schema,
    lazy_ensure,
    page_error,
    run_extract_async,
    run_search,
    search_ok,
    use_keyless,
    web_hit,
)

logger = logging.getLogger(__name__)

_MISSING_KEY = (
    "PARALLEL_API_KEY environment variable not set. "
    "Get your API key at https://parallel.ai"
)
_CLIENT_CACHE_LOCK = threading.Lock()
_CLIENT_CACHE_FINGERPRINT_KEY = secrets.token_bytes(32)


def _get_parallel_env(name: str) -> str:
    """Resolve one profile value without authoritative-scope fallback leakage."""
    from agent import secret_scope

    if (
        secret_scope.current_secret_scope() is not None
        or secret_scope.is_multiplex_active()
    ):
        return (secret_scope.get_secret(name) or "").strip()

    from agent.web_search_provider import get_provider_env

    return get_provider_env(name)


def _get_parallel_api_key() -> str:
    return _get_parallel_env("PARALLEL_API_KEY")


def _credential_id(api_key: str) -> bytes:
    """Return a process-local, non-reversible identity for one API key."""
    return hashlib.blake2b(
        api_key.encode("utf-8"),
        digest_size=16,
        key=_CLIENT_CACHE_FINGERPRINT_KEY,
    ).digest()


def _ensure_parallel_sdk_installed() -> None:
    lazy_ensure("search.parallel")


def _client(slot: str, identity_slot: str, cls_name: str) -> Any:
    import tools.web_tools as web_tools

    api_key = _get_parallel_api_key()
    if not api_key:
        raise ValueError(_MISSING_KEY)

    credential_id = _credential_id(api_key)
    with _CLIENT_CACHE_LOCK:
        cached = getattr(web_tools, slot, None)
        cached_id = getattr(web_tools, identity_slot, None)
        if (
            cached is not None
            and isinstance(cached_id, bytes)
            and hmac.compare_digest(cached_id, credential_id)
        ):
            return cached

        _ensure_parallel_sdk_installed()
        import parallel  # deliberately lazy

        client = getattr(parallel, cls_name)(api_key=api_key)
        setattr(web_tools, slot, client)
        setattr(web_tools, identity_slot, credential_id)
        return client


def _get_sync_client() -> Any:
    return _client("_parallel_client", "_parallel_client_credential_id", "Parallel")


def _get_async_client() -> Any:
    return _client(
        "_async_parallel_client",
        "_async_parallel_client_credential_id",
        "AsyncParallel",
    )


def _reset_clients_for_tests() -> None:
    import tools.web_tools as web_tools

    with _CLIENT_CACHE_LOCK:
        web_tools._parallel_client = None
        web_tools._async_parallel_client = None
        web_tools._parallel_client_credential_id = None
        web_tools._async_parallel_client_credential_id = None


def _resolve_search_mode(
    configured_mode: str | None = None,
    legacy_env_mode: str | None = None,
) -> str:
    """Resolve an explicit GA mode or migrate a legacy beta mode."""
    configured = (configured_mode or "").lower().strip()
    if configured:
        aliases = {"agentic": "advanced", "one-shot": "basic"}
        accepted = {"turbo", "fast", "basic", "advanced", *aliases}
        return aliases.get(configured, configured) if configured in accepted else "advanced"

    legacy = (legacy_env_mode or "agentic").lower().strip()
    return {
        "fast": "basic",
        "one-shot": "basic",
        "agentic": "advanced",
    }.get(legacy, "advanced")


class ParallelWebSearchProvider(BaseWebSearchProvider):
    """Parallel.ai GA search + async extract provider."""

    NAME = "parallel"
    DISPLAY_NAME = "Parallel"
    KEY_ENV = "PARALLEL_API_KEY"
    EXTRACT = True
    KEYLESS = True

    def is_available(self) -> bool:
        return bool(_get_parallel_api_key())

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        def _body() -> Dict[str, Any]:
            api_key = _get_parallel_api_key()
            if use_keyless("parallel", api_key):
                return keyless_search("Parallel", "parallel", query, limit, logger)

            import tools.web_tools as web_tools

            configured_mode = (
                web_tools._load_web_config().get("parallel_search_mode") or ""
            )
            legacy_env_mode = None
            if not configured_mode.strip():
                legacy_env_mode = _get_parallel_env("PARALLEL_SEARCH_MODE")
            mode = _resolve_search_mode(configured_mode, legacy_env_mode)
            logger.info(
                "Parallel search query_sha256=%s (mode=%s, limit=%d)",
                query_fingerprint(query)[:12],
                mode,
                limit,
            )
            response = _get_sync_client().search(
                search_queries=[query],
                objective=query,
                mode=mode,
                advanced_settings={"max_results": min(limit, SEARCH_LIMIT_CAP)},
            )
            return search_ok(
                [
                    web_hit(
                        result.url or "",
                        result.title or "",
                        " ".join(result.excerpts or []),
                        index + 1,
                    )
                    for index, result in enumerate(response.results or [])
                ]
            )

        return run_search("Parallel", logger, _body, sdk=True)

    async def extract(self, urls: List[str], **kwargs: Any) -> List[Dict[str, Any]]:
        async def _body() -> List[Dict[str, Any]]:
            api_key = _get_parallel_api_key()
            if use_keyless("parallel", api_key):
                return await asyncio.to_thread(
                    keyless_extract, "Parallel", "parallel", urls, logger
                )
            logger.info("Parallel extract: %d URL(s)", len(urls))
            response = await _get_async_client().extract(
                urls=urls,
                advanced_settings={"full_content": True},
            )
            results = [
                document(
                    result.url or "",
                    result.title or "",
                    result.full_content or "\n\n".join(result.excerpts or []),
                )
                for result in response.results or []
            ]
            return results + [
                {
                    **page_error(
                        error.url or "",
                        error.content or error.error_type or "extraction failed",
                    ),
                    "metadata": {"sourceURL": error.url or ""},
                }
                for error in response.errors or []
            ]

        return await run_extract_async("Parallel", logger, urls, _body, sdk=True)

    def get_setup_schema(self) -> Dict[str, Any]:
        return keyless_variant_schema(
            "Parallel",
            "PARALLEL_API_KEY",
            "https://parallel.ai",
            free_tag=(
                "Objective-tuned search + page extraction on Parallel's anonymous "
                "free tier. Rate-limited under burst load."
            ),
            paid_tag=(
                "Objective-tuned search + parallel page extraction via the "
                "Parallel GA SDK. Unthrottled, guaranteed service."
            ),
        )


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
_PLUGIN_COMPAT_LAZY = {
    "WebSearchProvider": ("agent.web_search_provider", "WebSearchProvider"),
}


def __getattr__(name):
    target = _PLUGIN_COMPAT_LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    from hermes_cli.plugin_compat import warn_once

    warn_once(__name__, name, *target)
    return getattr(importlib.import_module(target[0]), target[1])


# ---- END PLUGIN-COMPAT ----
