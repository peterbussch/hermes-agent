"""SearXNG search via a user-hosted instance (``/search?format=json``).

Search-only — SearXNG aggregates upstream engines but does not fetch URLs.
Env: ``SEARXNG_URL=http://localhost:8080``.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

import httpx

from agent.web_search_provider import query_fingerprint
from plugins.web._common import BaseWebSearchProvider, provider_env, search_fail, setup_schema

logger = logging.getLogger(__name__)


class SearXNGWebSearchProvider(BaseWebSearchProvider):
    """Search via a user-hosted SearXNG instance."""

    NAME = "searxng"
    DISPLAY_NAME = "SearXNG"
    KEY_ENV = "SEARXNG_URL"

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        base_url = provider_env("SEARXNG_URL").rstrip("/")
        if not base_url:
            return search_fail("SEARXNG_URL is not set")
        query_sha256 = query_fingerprint(query)
        try:
            response = httpx.get(
                f"{base_url}/search", params={"q": query, "format": "json", "pageno": 1},
                headers={"Accept": "application/json"}, timeout=15,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.warning("SearXNG HTTP error: status=%d query_sha256=%s", exc.response.status_code, query_sha256[:12])
            return search_fail(f"SearXNG returned HTTP {exc.response.status_code}")
        except httpx.RequestError as exc:
            logger.warning("SearXNG request error: type=%s query_sha256=%s", type(exc).__name__, query_sha256[:12])
            return search_fail(f"Could not reach SearXNG at {base_url} ({type(exc).__name__})")
        try:
            data = response.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("SearXNG response parse error: %s", exc)
            return search_fail("Could not parse SearXNG response as JSON")
        raw_results = data.get("results", [])
        # SearXNG may return a score field; sort descending and cap to limit.
        sorted_results = sorted(raw_results, key=lambda r: float(r.get("score", 0)), reverse=True)[:limit]
        web_results = [
            {
                "title": str(row.get("title", "")), "url": str(row.get("url", "")),
                "description": str(row.get("content", "")), "position": index + 1,
                "engine": str(row.get("engine", "")),
                "engines": [str(engine) for engine in (row.get("engines") or [])],
            }
            for index, row in enumerate(sorted_results)
        ]
        logger.info("SearXNG search query_sha256=%s: %d results (from %d raw, limit %d)",
                    query_sha256[:12], len(web_results), len(raw_results), limit)
        return {"success": True, "data": {"web": web_results,
                                               "unresponsive_engines": data.get("unresponsive_engines", [])}}

    def get_setup_schema(self) -> Dict[str, Any]:
        return setup_schema(
            "SearXNG", "free · self-hosted", "Free, privacy-respecting metasearch. Point SEARXNG_URL at your instance.",
            "SEARXNG_URL", "SearXNG instance URL (e.g. http://localhost:8080)", "https://searx.space/",
        )


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.
import os  # noqa: F401,E402


_PLUGIN_COMPAT_LAZY = {
    'WebSearchProvider': ('agent.web_search_provider', 'WebSearchProvider'),
}


def __getattr__(name):  # PEP 562 — lazy so no import cycles
    target = _PLUGIN_COMPAT_LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib
    from hermes_cli.plugin_compat import warn_once
    warn_once(__name__, name, *target)
    return getattr(importlib.import_module(target[0]), target[1])
# ---- END PLUGIN-COMPAT ----
