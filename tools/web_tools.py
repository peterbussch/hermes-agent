#!/usr/bin/env python3
"""Generic web_search / web_extract tools over pluggable backends.

Backend is selected during ``hermes tools`` (``web.backend`` in config.yaml; per
capability via ``web.search_backend`` / ``web.extract_backend``). Every vendor
implementation lives in ``plugins/web/<vendor>/provider.py`` and registers with
``agent.web_search_registry``; this module owns selection, safety gates,
caching, keyless rescue, and the truncate-and-store result pipeline.
Debug: ``WEB_TOOLS_DEBUG=true`` writes ``logs/web_tools_debug_<UUID>.json``.
"""

import asyncio
import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

import httpx

from agent.web_search_provider import query_fingerprint

# Per-vendor client cache slots; plugins read/write these via tools.web_tools (tests reset them to None).
_firecrawl_client = _firecrawl_client_config = _parallel_client = _async_parallel_client = _exa_client = None

from plugins.web.firecrawl.provider import (
    _is_tool_gateway_ready,
    check_firecrawl_api_key,
)
from tools.debug_helpers import DebugSession
from tools.tool_backend_helpers import NOUS_MANAGED_PROVIDER, selection_exists
from tools.url_safety import async_is_safe_url
from tools.web_tools_extract import (
    _extract_safe_urls,
    _merge_in_order,
    _no_provider_error,
    _resolve_extract_provider,
    _result_entry,
    _strict_selection_error,
    _validate_extract_urls,
)
from tools.web_tools_rescue import _rescue_eligible, _rescue_extract, _rescue_search
from tools.web_tools_truncate import (
    _effective_char_limit,
    _trim_results,
    _truncate_results,
    convert_base64_images_to_links,
)

logger = logging.getLogger(__name__)


# ─── Backend Selection ────────────────────────────────────────────────────────

def _env_value(name: str) -> str:
    """Resolve ``name`` via the config-aware env layer (``hermes config set`` values), then process env.

    Mirrors the SearXNG provider's ``_searxng_url()`` so that values set through Hermes' config/.env layer
    (``hermes config set``, ``hermes tools``) are honored here too — not just raw process-env exports.
    Without this, a config-only ``SEARXNG_URL`` (or any provider key) leaves the backend auto-detect cascade
    and ``check_web_api_key()`` blind to it. See #34290.
    """
    try:
        from hermes_cli.config import get_env_value
        val = get_env_value(name)
    except Exception:
        val = None
    return ((os.getenv(name, "") if val is None else val) or "").strip()


def _has_env(name: str) -> bool:
    return bool(_env_value(name))


def _load_web_config() -> dict:
    """Load the ``web:`` section from config.yaml; always a dict (a null section yields ``{}``)."""
    try:
        from hermes_cli.config import load_config
        return load_config().get("web") or {}
    except Exception:
        return {}


def _configured_backend(key: str = "backend") -> str:
    """Lower-cased, stripped ``web.<key>`` value ("" when unset/null)."""
    return (_load_web_config().get(key) or "").lower().strip()


def _registry_call(func_name: str, default, *args):
    """``agent.web_search_registry.<func_name>(*args)``, or *default* if it raised (registry never fatal)."""
    try:
        import agent.web_search_registry as registry_mod
        return getattr(registry_mod, func_name)(*args)
    except Exception as exc:  # noqa: BLE001 — registry optional; never fatal
        logger.debug("web provider registry %s%r failed: %s", func_name, args, exc)
        return default


def _registered_web_provider(backend: str):
    """Plugin-registered web provider by name, or ``None``."""
    return _registry_call("get_provider", None, backend) if backend else None


def _list_registered_web_providers():
    """All plugin-registered web providers (empty list on failure)."""
    return _registry_call("list_providers", [])


def _probe(provider, method: str, context: str = "") -> Optional[bool]:
    """``bool(provider.<method>())``, or ``None`` if it raised (a broken provider is unavailable; *context* is
    appended to the debug log line, e.g. " during readiness check")."""
    try:
        return bool(getattr(provider, method)())
    except Exception as exc:  # noqa: BLE001 — a broken provider is "unavailable"
        name = getattr(provider, "name", provider)
        logger.debug("web provider %r.%s() raised%s: %s", name, method, context, exc)
        return None


def _get_backend() -> str:
    """Shared web backend name. A stored ``web.backend`` is returned as-is — no availability probe, no
    fallback — so a broken selection surfaces the vendor's honest error rather than silently rerouting.
    Autodetect runs ONLY when no web selection has ever been stored."""
    configured = _configured_backend()
    if configured:
        # "nous" (managed subscription) is serviced by firecrawl, routed through the managed Tool Gateway.
        return "firecrawl" if configured == NOUS_MANAGED_PROVIDER else configured
    if selection_exists("web"):
        # Selection exists (use_gateway / per-capability keys) but no shared name: firecrawl, no ladder.
        return "firecrawl"

    # Never-configured install. Explicit user credentials beat the managed-gateway probe (a Nous OAuth
    # token's tier may not grant web access; the gateway then fails at runtime with no fallback).
    # Free tiers trail paid.
    backend_candidates = (
        ("tavily", _has_env("TAVILY_API_KEY")), ("perplexity", _has_env("PERPLEXITY_API_KEY")),
        ("exa", _has_env("EXA_API_KEY")),
        ("parallel", _has_env("PARALLEL_API_KEY")), ("keenable", _has_env("KEENABLE_API_KEY")),
        ("firecrawl", _has_env("FIRECRAWL_API_KEY") or _has_env("FIRECRAWL_API_URL")),
        ("firecrawl", _is_tool_gateway_ready()), ("searxng", _has_env("SEARXNG_URL")),
        ("brave-free", _has_env("BRAVE_SEARCH_API_KEY")), ("ddgs", _ddgs_package_importable()),
    )
    for backend, available in backend_candidates:
        if available:
            return backend

    # Plugin-contributed providers (built-ins are covered above); probe the held object directly.
    for provider in _list_registered_web_providers():
        if provider.name not in _LEGACY_WEB_BACKENDS and _probe(provider, "is_available"):
            return provider.name

    # Keyless free tier — strictly last so it never pre-empts a keyed backend. Discovery must run
    # first: reachable from contexts that haven't loaded plugins (subprocess runs, delegate children).
    try:
        _ensure_web_plugins_loaded()
        from agent.web_search_registry import _keyless_preference, _keyless_tier_enabled
        if _keyless_tier_enabled():
            for name in _keyless_preference():
                provider = _registered_web_provider(name)
                if provider is not None and _probe(provider, "is_keyless_available"):
                    return name
    except Exception as exc:  # noqa: BLE001 — registry optional; never fatal
        logger.debug("keyless fallback walk failed: %s", exc)

    return "firecrawl"  # default (backward compat)


def _get_search_backend() -> str:
    """Backend for web_search: ``web.search_backend`` (strict, no probe) > ``web.backend`` > autodetect."""
    return _get_capability_backend("search")


def _get_extract_backend() -> str:
    """Backend for web_extract: ``web.extract_backend`` (strict, no probe) > ``web.backend`` > autodetect."""
    return _get_capability_backend("extract")


def _get_capability_backend(capability: str) -> str:
    """Resolve an explicit capability backend, otherwise choose a capable provider."""
    cfg = _load_web_config()
    specific = str(cfg.get(f"{capability}_backend") or "").lower().strip()
    if specific:
        provider = _registered_web_provider(specific)
        if provider is not None:
            supports = _probe(provider, f"supports_{capability}")
            if supports:
                return specific
        if _is_backend_available(specific):
            return specific
    if str(cfg.get("backend") or "").strip():
        return _get_backend()
    try:
        import agent.web_search_registry as registry_mod
        candidates = registry_mod.get_provider_candidates(capability=capability)
        if candidates:
            return candidates[0].name
    except Exception as exc:  # noqa: BLE001
        logger.debug("capability-aware backend selection failed: %s", exc)
    return _get_backend()


def _ddgs_package_importable() -> bool:
    """ddgs is the only backend gated on package presence; single symbol so tests can patch it."""
    try:
        import ddgs  # noqa: F401
        return True
    except ImportError:
        return False


def _xai_available() -> bool:
    # Cheap probe only (env var OR auth.json OAuth): resolve_xai_http_credentials() may hit the network.
    try:
        from tools.xai_http import has_xai_credentials
        return has_xai_credentials()
    except Exception:
        return False


# Built-in backends -> cheap availability probes; any other name is a plugin provider resolved via the
# registry's ``is_available()``. Lambdas so test patches of module-level helpers (_ddgs_package_importable,
# check_firecrawl_api_key) are honored at call time. ``xai`` is probed via has_xai_credentials(), not a
# registered provider, though the registry's _LEGACY_PREFERENCE omits it — drop it if xai ever registers.
_BUILTIN_AVAILABILITY = {
    "exa": lambda: _has_env("EXA_API_KEY"),
    "parallel": lambda: _has_env("PARALLEL_API_KEY"),
    "keenable": lambda: _has_env("KEENABLE_API_KEY"),
    "firecrawl": lambda: check_firecrawl_api_key(),
    "tavily": lambda: _has_env("TAVILY_API_KEY")
    or any(_configured_backend(k) == "tavily" for k in ("backend", "search_backend", "extract_backend")),
    "perplexity": lambda: _has_env("PERPLEXITY_API_KEY"),
    "searxng": lambda: _has_env("SEARXNG_URL"),
    "brave-free": lambda: _has_env("BRAVE_SEARCH_API_KEY"),
    "ddgs": lambda: _ddgs_package_importable(),
    "xai": _xai_available,
}
_LEGACY_WEB_BACKENDS = frozenset(_BUILTIN_AVAILABILITY)


def _is_backend_available(backend: str) -> bool:
    """True when *backend* is usable — the single availability chokepoint. Non-legacy names delegate to the
    registered provider's ``is_available()`` (unregistered names fall through); built-ins use cheap probes.

    For plugin-registered backends (any name outside :data:`_LEGACY_WEB_BACKENDS`), availability is
    delegated to the provider's ``is_available()`` via the web_search_registry. This is the single
    chokepoint through which ``_get_backend``, ``_get_capability_backend``, and ``check_web_api_key`` all
    resolve availability — fixing custom-provider discovery for every caller at once (issues #28651, #31873,
    #32698). Built-in backends keep their cheap hardcoded probes below.
    """
    backend = (backend or "").lower().strip()
    provider = None if backend in _LEGACY_WEB_BACKENDS else _registered_web_provider(backend)
    if provider is not None:
        return _probe(provider, "is_available") or False
    probe = _BUILTIN_AVAILABILITY.get(backend)
    return probe() if probe else False


# ─── Firecrawl Client ──────────────────────────────────────────────────────── After PR #25182, the
# firecrawl client, lazy SDK proxy, dual-auth config resolution, response normalizers, and
# check_firecrawl_api_key() all live in plugins.web.firecrawl.provider.
def _web_requires_env() -> list[str]:
    """Tool-registry metadata env vars for the web backends. Gateway vars are always listed: gating them
    on ``managed_nous_tools_enabled()`` cost a synchronous portal HTTP refresh at every CLI startup.
    Contract: set var -> tool sees it; extras are harmless for the not-logged-in."""
    return [
        "EXA_API_KEY", "PARALLEL_API_KEY", "TAVILY_API_KEY", "PERPLEXITY_API_KEY", "KEENABLE_API_KEY", "FIRECRAWL_API_KEY",
        "FIRECRAWL_API_URL", "FIRECRAWL_GATEWAY_URL", "TOOL_GATEWAY_DOMAIN", "TOOL_GATEWAY_SCHEME",
        "TOOL_GATEWAY_USER_TOKEN",
    ]

_debug = DebugSession("web_tools", env_var="WEB_TOOLS_DEBUG")


# ─── Dispatch ─────────────────────────────────────────────────────────────────

# ─── Exa / Parallel inline helpers — moved into plugins ────────────────────── After PR #25182, the exa
# client + search/extract and parallel client + search/extract helpers all live in their respective plugins:
# - plugins/web/exa/provider.py - plugins/web/parallel/provider.py Both plugins register through
# agent.web_search_registry and the dispatchers in this file resolve them via get_active_*_provider().
def _ensure_web_plugins_loaded() -> None:
    """Idempotently run plugin discovery so the web registry is populated. Dispatch is reachable from contexts
    that never triggered discovery (subprocess agent runs, delegate children, scripts); without it a
    configured backend yields a misleading "No web ... provider" error.

    Every bundled web provider (brave-free, ddgs, searxng, exa, parallel, tavily, firecrawl, keenable)
    registers itself via ``plugins/web/<vendor>/__init__.py`` during plugin discovery. Tool dispatch can be
    reached from contexts that haven't already triggered discovery — subprocess agent runs, delegate
    children, standalone scripts, certain test paths — and without it the registry is empty and
    ``get_provider('firecrawl')`` returns ``None`` even when the user has ``web.extract_backend: firecrawl``
    configured and ``FIRECRAWL_API_KEY`` set. See #27580.
    """
    try:
        from hermes_cli.plugins import _ensure_plugins_discovered
        _ensure_plugins_discovered()
    except Exception as exc:  # noqa: BLE001
        # Warning, not debug: a broken plugin import is otherwise invisible.
        logger.warning("Web plugin discovery failed (non-fatal): %s", exc)


def _redact_web_failure(value: Any, *, query: Optional[str] = None) -> str:
    """Redact credentials and replace plaintext query variants with a digest marker."""
    from urllib.parse import quote, quote_plus

    from agent.redact import redact_sensitive_text

    redacted = redact_sensitive_text(str(value), force=True)
    if not query:
        return redacted
    marker = f"[query sha256={query_fingerprint(query)[:12]}]"
    for variant in sorted({query, quote(query, safe=""), quote_plus(query, safe="")}, key=len, reverse=True):
        if variant:
            redacted = redacted.replace(variant, marker)
    return redacted


_SEARCH_FAILURE_THRESHOLD = 3
_SEARCH_COOLDOWN_SECONDS = 120.0
_SEARCH_CIRCUIT_LOCK = threading.Lock()
_SEARCH_CIRCUIT: Dict[str, Dict[str, float]] = {}


def _reset_search_circuit_for_tests() -> None:
    with _SEARCH_CIRCUIT_LOCK:
        _SEARCH_CIRCUIT.clear()


def _search_circuit_open(provider_name: str) -> bool:
    now = time.monotonic()
    with _SEARCH_CIRCUIT_LOCK:
        state = _SEARCH_CIRCUIT.get(provider_name)
        if not state:
            return False
        if state.get("cooldown_until", 0.0) > now:
            return True
        if state.get("cooldown_until", 0.0):
            if state.get("half_open_in_flight", 0.0):
                return True
            state["half_open_in_flight"] = 1.0
        return False


def _record_search_provider_failure(provider_name: str) -> None:
    now = time.monotonic()
    with _SEARCH_CIRCUIT_LOCK:
        state = _SEARCH_CIRCUIT.setdefault(
            provider_name, {"failures": 0.0, "cooldown_until": 0.0, "half_open_in_flight": 0.0},
        )
        if state.get("half_open_in_flight", 0.0):
            state.update(failures=float(_SEARCH_FAILURE_THRESHOLD),
                         cooldown_until=now + _SEARCH_COOLDOWN_SECONDS, half_open_in_flight=0.0)
            return
        state["failures"] += 1.0
        if state["failures"] >= _SEARCH_FAILURE_THRESHOLD:
            state.update(cooldown_until=now + _SEARCH_COOLDOWN_SECONDS, half_open_in_flight=0.0)


def _record_search_provider_operational(provider_name: str) -> None:
    with _SEARCH_CIRCUIT_LOCK:
        _SEARCH_CIRCUIT.pop(provider_name, None)


def _usable_search_results(results: Any) -> List[Dict[str, Any]]:
    """Keep concrete HTTP(S) search results and discard interstitial junk."""
    from urllib.parse import urlsplit

    from plugins.web.local_browser.content_quality import classify_extraction

    usable: List[Dict[str, Any]] = []
    for item in results if isinstance(results, list) else []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or item.get("href") or "").strip()
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            continue
        title = str(item.get("title") or "").strip()
        description = str(item.get("description") or item.get("content") or item.get("snippet") or "").strip()
        quality = classify_extraction({"url": url, "title": title, "content": description or title})
        if quality["content_class"] not in {"empty", "placeholder", "login_wall", "challenge"}:
            usable.append(item)
    return usable


def _search_attempt_record(provider_name: str, response: Any, *, query: str) -> Dict[str, Any]:
    if not isinstance(response, dict):
        return {"provider": provider_name, "status": "error",
                "error": f"invalid response type: {type(response).__name__}"}
    if not response.get("success"):
        return {"provider": provider_name, "status": "error",
                "error": _redact_web_failure(response.get("error") or "search failed", query=query)}
    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    web = data.get("web") if isinstance(data, dict) else None
    usable = _usable_search_results(web)
    if usable:
        return {"provider": provider_name, "status": "success", "results_count": len(usable)}
    record: Dict[str, Any] = {
        "provider": provider_name, "status": "empty", "error": "provider returned no usable search results",
    }
    details = {key: value for key, value in data.items() if key != "web" and value not in (None, "", [], {})}
    if details:
        record["details"] = details
    diagnostic = any(any(marker in str(key).lower() for marker in ("unresponsive", "error", "fail", "blocked", "rate"))
                     for key in details)
    if diagnostic or (isinstance(web, list) and bool(web)):
        record["status"] = "degraded"
        record["discarded_results_count"] = len(web) if isinstance(web, list) else 0
    return record


def _search_with_fallback(providers, query: str, limit: int) -> Dict[str, Any]:
    """Try providers until one returns at least one concrete search result."""
    provenance: List[Dict[str, Any]] = []
    for position, provider in enumerate(providers):
        if _search_circuit_open(provider.name):
            provenance.append({"provider": provider.name, "status": "circuit_open",
                               "error": "provider temporarily skipped after repeated failures"})
            continue
        try:
            response = provider.search(query, limit)
        except Exception as exc:  # noqa: BLE001
            _record_search_provider_failure(provider.name)
            provenance.append({"provider": provider.name, "status": "error",
                               "error": _redact_web_failure(exc, query=query)})
            continue
        attempt = _search_attempt_record(provider.name, response, query=query)
        provenance.append(attempt)
        if attempt["status"] in {"error", "degraded"}:
            _record_search_provider_failure(provider.name)
        else:
            _record_search_provider_operational(provider.name)
        if attempt["status"] != "success":
            continue
        response = dict(response)
        data = dict(response.get("data") or {})
        data["web"] = _usable_search_results(data.get("web"))
        data.update(provider=provider.name, fallback=position > 0, provenance=list(provenance))
        response["data"] = data
        return response
    errors = "; ".join(f"{item['provider']}: {item.get('error', item['status'])}" for item in provenance)
    return {"success": False, "error": f"All web search providers were unusable ({errors})",
            "provider": None, "fallback": len(provenance) > 1, "provenance": provenance}


def _provider_chain(primary, candidates):
    if primary is None:
        return list(candidates)
    return [primary] + [candidate for candidate in candidates
                        if candidate is not primary and candidate.name != primary.name]


async def _extract_known_json_url(url: str) -> Optional[Dict[str, Any]]:
    """Fetch the exact public FXTwitter JSON endpoint before generic readers."""
    from urllib.parse import urlsplit

    parsed = urlsplit(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() != "api.fxtwitter.com":
        return None
    from tools.website_policy import check_website_access
    blocked = check_website_access(url)
    if blocked:
        return {"url": url, "title": "", "content": "", "raw_content": "", "error": blocked["message"],
                "blocked_by_policy": {key: blocked[key] for key in ("host", "rule", "source")}}
    try:
        timeout = httpx.Timeout(10.0, connect=3.0, read=10.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            response = await client.get(url, headers={
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126.0 Safari/537.36",
            })
            if 300 <= response.status_code < 400:
                raise ValueError("direct JSON endpoint redirected")
            response.raise_for_status()
            payload = response.json()
        tweet = payload.get("tweet") if isinstance(payload, dict) else None
        expected_id = parsed.path.rstrip("/").split("/")[-1]
        if not isinstance(tweet, dict) or str(tweet.get("id") or "") != expected_id or not str(tweet.get("text") or "").strip():
            raise ValueError("tweet envelope was missing or mismatched")
        content = json.dumps(payload, indent=2, ensure_ascii=False)
        return {"url": url, "title": f"FXTwitter status {expected_id}", "content": content,
                "raw_content": content, "metadata": {"provider": "direct_json", "sourceURL": url, "validated": True}}
    except Exception as exc:  # noqa: BLE001
        return {"url": url, "title": "", "content": "", "raw_content": "",
                "error": f"Direct FXTwitter JSON extract failed: {_redact_web_failure(exc)}"}


def _quality_score(result: Dict[str, Any]) -> float:
    base = {"article": 10.0, "partial": 5.0, "placeholder": 1.0, "login_wall": 1.0,
            "challenge": 1.0, "empty": 0.0}.get(result.get("content_class"), 0.0)
    return base + float(result.get("completeness") or 0.0)


async def _extract_with_fallback(providers, urls: List[str], *, format: str) -> List[Dict[str, Any]]:
    """Retry only degraded URLs, then use keyless rescue if the whole batch still failed."""
    from plugins.web.local_browser.content_quality import annotate_extraction

    attempts: Dict[int, List[Dict[str, Any]]] = {i: [] for i in range(len(urls))}
    best: Dict[int, Dict[str, Any]] = {}
    selected_provider: Dict[int, str] = {}
    unresolved: List[int] = []
    for index, url in enumerate(urls):
        direct = await _extract_known_json_url(url)
        if direct is None:
            unresolved.append(index)
            continue
        candidate = annotate_extraction(direct)
        if not candidate.get("error") and (candidate.get("metadata") or {}).get("validated") is True:
            candidate.update(content_class="article", status="success", completeness=1.0, blocker=None)
        attempt = {"provider": "direct_json", "status": candidate["status"],
                   "content_class": candidate["content_class"], "completeness": candidate["completeness"],
                   "blocker": candidate["blocker"]}
        if candidate.get("error"):
            attempt["error"] = _redact_web_failure(candidate["error"])
        attempts[index].append(attempt)
        best[index], selected_provider[index] = candidate, "direct_json"
        if candidate["content_class"] != "article" and not candidate.get("blocked_by_policy"):
            unresolved.append(index)

    primary_indices = list(unresolved)
    primary_results: Dict[int, Dict[str, Any]] = {}
    for provider_position, provider in enumerate(providers):
        if not unresolved:
            break
        requested_indices = list(unresolved)
        requested_urls = [urls[index] for index in requested_indices]
        try:
            raw = await _extract_safe_urls(
                provider,
                requested_urls,
                format,
                rescue=False,
                use_cache=provider_position == 0,
            )
            if not isinstance(raw, list):
                raise TypeError(f"{provider.name} returned {type(raw).__name__}, expected list")
        except Exception as exc:  # noqa: BLE001
            display = getattr(provider, "display_name", provider.name)
            raw = [
                {
                    "url": url,
                    "title": "",
                    "content": "",
                    "raw_content": "",
                    "error": f"{display} extract failed: {_redact_web_failure(exc)}",
                }
                for url in requested_urls
            ]

        by_url: Dict[str, Dict[str, Any]] = {}
        for item in raw:
            if not isinstance(item, dict):
                continue
            returned_url = str(item.get("url") or "").strip()
            if returned_url in requested_urls and returned_url not in by_url:
                by_url[returned_url] = item

        next_unresolved: List[int] = []
        for index in requested_indices:
            requested_url = urls[index]
            matched = by_url.get(requested_url)
            if matched is None:
                candidate = {"url": requested_url, "title": "", "content": "", "raw_content": "",
                             "error": "Extract backend returned no result for this URL"}
            else:
                candidate = dict(matched)
                candidate["url"] = requested_url
            candidate = annotate_extraction(candidate)
            if provider_position == 0:
                primary_results[index] = candidate
            attempt = {"provider": provider.name, "status": candidate["status"],
                       "content_class": candidate["content_class"], "completeness": candidate["completeness"],
                       "blocker": candidate["blocker"]}
            if candidate.get("error"):
                attempt["error"] = _redact_web_failure(candidate["error"])
            attempts[index].append(attempt)
            if index not in best or _quality_score(candidate) > _quality_score(best[index]):
                best[index], selected_provider[index] = candidate, provider.name
            if candidate["content_class"] != "article" and not candidate.get("blocked_by_policy"):
                next_unresolved.append(index)
        unresolved = next_unresolved

    # Preserve upstream's one-shot keyless rescue, but only after the bounded
    # configured-provider chain has had a chance to recover the batch. A
    # partial success is a page-level problem and must not trigger wholesale
    # re-fetching through the ring.
    if (
        providers
        and primary_indices
        and len(primary_indices) == len(urls)
        and set(unresolved) == set(primary_indices)
        and all(primary_results.get(index, {}).get("error") for index in primary_indices)
        and _rescue_eligible(providers[0])
    ):
        original = [primary_results[index] for index in primary_indices]
        rescued = await asyncio.to_thread(
            _rescue_extract, providers[0].name,
            [urls[index] for index in primary_indices], original,
        )
        for index, candidate_raw in zip(primary_indices, rescued):
            candidate = annotate_extraction(dict(candidate_raw))
            attempt = {
                "provider": "keyless_rescue",
                "status": candidate["status"],
                "content_class": candidate["content_class"],
                "completeness": candidate["completeness"],
                "blocker": candidate["blocker"],
            }
            if candidate.get("error"):
                attempt["error"] = _redact_web_failure(candidate["error"])
            attempts[index].append(attempt)
            if not candidate.get("error"):
                best[index], selected_provider[index] = candidate, "keyless_rescue"

    results: List[Dict[str, Any]] = []
    for index, url in enumerate(urls):
        selected = dict(best.get(index, annotate_extraction({
            "url": url, "title": "", "content": "", "raw_content": "",
            "error": "No extract provider returned a result",
        })))
        provider_name = selected_provider.get(index, "")
        first_provider = attempts[index][0]["provider"] if attempts[index] else ""
        selected.update(provider=provider_name, fallback=bool(first_provider and provider_name != first_provider),
                        fallback_attempted=len(attempts[index]) > 1, provenance=attempts[index])
        results.append(selected)
    return results


def _finish_debug(call_name: str, debug_call_data: dict, error_msg: Optional[str] = None) -> Optional[str]:
    """Log the call into the debug session; with *error_msg*, record it and return its ``tool_error`` envelope."""
    if error_msg is not None:
        logger.debug("%s", error_msg)
        debug_call_data["error"] = error_msg
    _debug.log_call(call_name, debug_call_data)
    _debug.save()
    return None if error_msg is None else tool_error(error_msg)


def web_search_tool(query: str, limit: int = 5) -> str:
    """Search the web via the configured backend.

    Returns a JSON string ``{"success": bool, "data": {"web": [{"title", "url", "description", "position"},
    ...]}}`` (metadata only — use web_extract_tool for page content) or ``{"success": false, "error": ...}``.
    """
    try:
        limit = min(max(int(limit), 1), 100)
    except (TypeError, ValueError):
        limit = 5
    query_sha256 = query_fingerprint(query)
    debug_call_data = {
        "parameters": {"query_sha256": query_sha256, "limit": limit}, "error": None, "results_count": 0,
        "original_response_size": 0, "final_response_size": 0,
    }

    try:
        from tools.interrupt import is_interrupted
        if is_interrupted():
            return tool_error("Interrupted", success=False)
        # Sync only — every provider's search() is sync.
        _ensure_web_plugins_loaded()
        from agent.web_search_registry import (
            get_active_search_provider,
            get_provider_candidates,
        )
        from agent.web_search_registry import (
            get_provider as _wsp_get_provider,
        )
        backend = _get_search_backend()
        provider = _wsp_get_provider(backend) if backend else None
        if provider is None or not provider.supports_search():
            if provider is None and backend and selection_exists("web"):
                error_text = debug_call_data["error"] = _strict_selection_error("search", backend)
                _finish_debug("web_search_tool", debug_call_data)
                return json.dumps({"success": False, "error": error_text}, indent=2, ensure_ascii=False)
            # Never-configured install: legacy availability-walked autodetect.
            provider = get_active_search_provider()

        if provider is None:
            fallback = "No web search provider configured. Run `hermes tools` to set one up."
            response_data = {"success": False, "error": _no_provider_error("search", fallback)}
        else:
            logger.info("Web search via %s: query_sha256=%s (limit: %d)",
                        provider.name, query_sha256[:12], limit)
            candidates = _provider_chain(
                provider, get_provider_candidates(capability="search", configured=provider.name),
            )
            response_data = _memoized_search(provider, query, limit, providers=candidates)

        debug_call_data["results_count"] = len(response_data.get("data", {}).get("web", []))
        result_json = json.dumps(response_data, indent=2, ensure_ascii=False)
        debug_call_data["final_response_size"] = len(result_json)
        if not response_data.get("success", False):
            debug_call_data["error"] = response_data.get("error")
        _finish_debug("web_search_tool", debug_call_data)
        return result_json
    except Exception as e:
        return _finish_debug(
            "web_search_tool", debug_call_data,
            f"Error searching web: {_redact_web_failure(e, query=query)}",
        )


def _memoized_search(provider, query: str, limit: int, *, providers=None) -> dict:
    """Memoize a primary success around bounded fallback and one-shot rescue.

    A configured-provider fallback or keyless rescue is deliberately not
    cached under the primary provider's key. That keeps both mechanisms
    stateless: every later call gets a fresh chance to use the configured
    backend.
    """
    from tools.web_result_cache import bucket_limit, search_memo, slice_search_response

    def _paid_search() -> tuple[dict, bool]:
        fetch_limit = bucket_limit(limit)
        resp = _search_with_fallback(providers or [provider], query, fetch_limit)
        if not resp.get("success") and _rescue_eligible(provider):
            rescued = _rescue_search(
                provider.name,
                _redact_web_failure(resp.get("error", ""), query=query),
                query,
                fetch_limit,
            )
            if rescued.get("success"):
                # Rescue is one-shot by contract: the next call must retry the
                # configured backend rather than inherit its circuit state.
                _record_search_provider_operational(provider.name)
            return rescued, True
        used_primary = (
            resp.get("success")
            and (resp.get("data") or {}).get("provider") == provider.name
            and not (resp.get("data") or {}).get("fallback")
        )
        return resp, not used_primary

    response_data = search_memo.lookup(provider.name, query, limit)
    if response_data is None:
        with search_memo.flight_lock(provider.name, query, limit):
            # Re-check inside the lock: a concurrent identical call may have stored.
            response_data = search_memo.lookup(provider.name, query, limit)
            if response_data is None:
                response_data, was_rescued = _paid_search()
                if not was_rescued:
                    search_memo.store(provider.name, query, limit, response_data)
    return slice_search_response(response_data, limit)


async def web_extract_tool(urls: List[Any], format: str = None, char_limit: Optional[int] = None) -> str:
    """Extract clean page content (no LLM) from URLs via the configured backend.

    Pages over ``char_limit`` (default web.extract_char_limit or 15000) are head+tail truncated with a footer
    pointing at the stored full text; inline base64 images become ``[IMAGE: alt]``. URLs carrying secrets are
    refused before any fetch; private-network URLs are blocked per entry. Returns JSON ``{"results": [...]}``.
    """
    normalized_urls, normalized_indices, invalid_urls, blocked = _validate_extract_urls(urls)
    if blocked is not None:
        return blocked
    debug_call_data = {
        "parameters": {"urls": normalized_urls, "format": format, "char_limit": char_limit}, "error": None,
        "pages_extracted": 0, "pages_truncated": 0, "original_response_size": 0, "final_response_size": 0,
        "truncation_metrics": [], "processing_applied": [],
    }

    try:
        logger.info("Extracting content from %d URL(s)", len(normalized_urls))
        # SSRF protection — filter private/internal URLs before any backend.
        safe_urls, safe_indices, ssrf_blocked = [], [], {}
        for index, url in zip(normalized_indices, normalized_urls):
            if await async_is_safe_url(url):
                safe_urls.append(url)
                safe_indices.append(index)
            else:
                ssrf_blocked[index] = _result_entry(
                    url, "Blocked: URL targets a private or internal network address"
                )

        results = []
        if safe_urls:
            _ensure_web_plugins_loaded()
            backend = _get_extract_backend()
            provider, error_json = _resolve_extract_provider(backend)
            if error_json is not None:
                return error_json
            from agent.web_search_registry import get_provider_candidates
            providers = _provider_chain(
                provider, get_provider_candidates(capability="extract", configured=provider.name),
            )
            results = await _extract_with_fallback(providers, safe_urls, format=format or "markdown")
        # Reconstruct input order across invalid, blocked, and provider entries (providers preserve
        # the order of the safe URL list they receive).
        if invalid_urls or ssrf_blocked:
            fixed = {**ssrf_blocked, **invalid_urls}
            results = _merge_in_order(len(urls), fixed, safe_indices, safe_urls, results)

        logger.info("Extracted content from %d pages", len(results))
        debug_call_data["pages_extracted"] = len(results)
        debug_call_data["original_response_size"] = len(json.dumps({"results": results}))
        debug_call_data["processing_applied"].append("truncate_and_store")
        _truncate_results(results, _effective_char_limit(char_limit), debug_call_data)
        trimmed = _trim_results(results)
        result_json = (
            json.dumps({"results": trimmed}, indent=2, ensure_ascii=False) if trimmed
            else tool_error("Content was inaccessible or not found")
        )
        # Belt-and-suspenders sweep of the serialized JSON: a provider may tuck a base64 blob in metadata.
        cleaned_result = convert_base64_images_to_links(result_json)
        debug_call_data["final_response_size"] = len(cleaned_result)
        debug_call_data["processing_applied"].append("base64_image_conversion")
        _finish_debug("web_extract_tool", debug_call_data)
        return cleaned_result
    except Exception as e:
        return _finish_debug("web_extract_tool", debug_call_data, f"Error extracting content: {str(e)}")


def _provider_is_ready(provider) -> bool:
    """True when *provider* is keyed-available OR keyless-capable, without raising.

    ``get_active_*_provider()`` returns an explicitly configured backend even when ``is_available()`` is
    False (so dispatch can emit a precise error), so readiness gates (tool check_fn, ``hermes doctor``)
    must probe for real. Keyless mode (Exa/Parallel free tier) is a working state, not a misconfig.

    See #78412.
    """
    if provider is None:
        return False
    ready = _probe(provider, "is_available", " during readiness check")
    if ready is None:  # broken provider == not ready; don't try the keyless probe
        return False
    return bool(ready or _probe(provider, "is_keyless_available", " during readiness check"))


def check_web_api_key() -> bool:
    """``check_fn`` gate for web_search / web_extract: is any web backend available?

    A plugin-registered provider reporting ``is_available()`` must light the tools up even with no
    built-in credentials; resolution funnels through :func:`_is_backend_available`.

    See #28651, #31873.
    """
    # Boolean OR over configured + built-ins — probe order is irrelevant here.
    candidates = [c for c in (_configured_backend(),) if c] + list(_LEGACY_WEB_BACKENDS)
    if any(_is_backend_available(backend) for backend in candidates):
        return True
    # Plugin path. Discovery must run first: check_fn fires at tool-registration time, before any dispatch.
    try:
        _ensure_web_plugins_loaded()
        from agent.web_search_registry import (
            get_active_extract_provider,
            get_active_search_provider,
        )
        return _provider_is_ready(get_active_search_provider()) or _provider_is_ready(
            get_active_extract_provider()
        )
    except Exception as exc:  # noqa: BLE001 — registry optional; never fatal
        logger.debug("web provider registry availability check failed: %s", exc)
        return False


# ─── Registry ─────────────────────────────────────────────────────────────────
from tools.registry import registry, tool_error

WEB_SEARCH_SCHEMA = {
    "name": "web_search",
    "description": "Search the web for information. Returns up to 5 results by default with titles, URLs, and descriptions. The query is passed through to the configured backend, so operators such as site:domain, filetype:pdf, intitle:word, -term, and \"exact phrase\" may work when the backend supports them.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query to look up on the web. You may include backend-supported operators such as site:example.com, filetype:pdf, intitle:word, -term, or \"exact phrase\"."
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of results to return. Defaults to 5.",
                "minimum": 1,
                "maximum": 100,
                "default": 5
            }
        },
        "required": ["query"]
    }
}

WEB_EXTRACT_SCHEMA = {
    "name": "web_extract",
    "description": "Extract content from web page URLs. Returns clean page content in markdown/text (no LLM summarization — fast). Also works with PDF URLs (arxiv papers, documents) — pass the PDF link directly. Pages within the char budget (default 15000) return whole; larger pages return a head+tail window with a footer telling you the full text's saved file path and the read_file call to page through the omitted middle. Inline images appear as [IMAGE: alt] placeholders; real image URLs are kept as links. If a URL fails or times out, use the browser tool instead.",
    "parameters": {
        "type": "object",
        "properties": {
            "urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of URLs to extract content from (max 5 URLs per call)",
                "maxItems": 5
            },
            "char_limit": {
                "type": "integer",
                "description": "Optional per-page character budget sent back (default 15000). Pages larger than this are head+tail truncated with the full text stored to disk. Raise it when you need more of a long page inline.",
                "minimum": 2000
            }
        },
        "required": ["urls"]
    }
}

registry.register(
    name="web_search", toolset="web", schema=WEB_SEARCH_SCHEMA,
    handler=lambda args, **kw: web_search_tool(args.get("query", ""), limit=args.get("limit", 5)),
    check_fn=check_web_api_key, requires_env=_web_requires_env(), emoji="🔍",
    max_result_size_chars=100_000,
)
registry.register(
    name="web_extract", toolset="web", schema=WEB_EXTRACT_SCHEMA,
    handler=lambda args, **kw: web_extract_tool(
        args.get("urls", [])[:5] if isinstance(args.get("urls"), list) else [], "markdown",
        char_limit=args.get("char_limit"),
    ),
    check_fn=check_web_api_key, requires_env=_web_requires_env(), is_async=True, emoji="📄",
    max_result_size_chars=100_000,
)


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.
import re  # noqa: F401,E402
import sys  # noqa: F401,E402
from typing import TYPE_CHECKING  # noqa: F401,E402

_PLUGIN_COMPAT_LAZY = {
    'DEFAULT_EXTRACT_CHAR_LIMIT': ('tools.web_tools_truncate', 'DEFAULT_EXTRACT_CHAR_LIMIT'),
    'Firecrawl': ('plugins.web.firecrawl.provider', 'Firecrawl'),
    'MAX_STORED_TEXT_CHARS': ('tools.web_tools_truncate', 'MAX_STORED_TEXT_CHARS'),
    'build_vendor_gateway_url': ('tools.managed_tool_gateway', 'build_vendor_gateway_url'),
    'managed_nous_tools_enabled': ('tools.tool_backend_helpers', 'managed_nous_tools_enabled'),
    'normalize_url_for_request': ('tools.url_safety', 'normalize_url_for_request'),
    'nous_tool_gateway_unavailable_message': ('tools.tool_backend_helpers', 'nous_tool_gateway_unavailable_message'),
    'prefers_gateway': ('tools.tool_backend_helpers', 'prefers_gateway'),
    'resolve_managed_tool_gateway': ('tools.managed_tool_gateway', 'resolve_managed_tool_gateway'),
    'sensitive_query_param_name': ('tools.url_safety', 'sensitive_query_param_name'),
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
