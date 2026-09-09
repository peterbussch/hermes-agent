"""Jina Reader content extraction — plugin form.

Jina Reader exposes a no-key HTTP endpoint at ``https://r.jina.ai/<url>`` that
returns readable markdown for a target page. This provider is extract-only and
is intended to be the fast free default before falling back to rendered local
browser extraction.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Dict, List

from agent.redact import redact_sensitive_text
from agent.web_search_provider import WebSearchProvider, get_provider_env

logger = logging.getLogger(__name__)


_READER_BASE_URL = "https://r.jina.ai"
_TITLE_RE = re.compile(r"^\s*#\s+(.+?)\s*$", re.MULTILINE)
_COOLDOWN_SECONDS = 300.0
_TRANSIENT_FAILURE_THRESHOLD = 2
_health_lock = threading.Lock()
_failure_streaks = {"rate_limit": 0, "transport": 0}
_cooldown_until = 0.0
_cooldown_reason = ""


def _redact(error: Any) -> str:
    return redact_sensitive_text(str(error), force=True)


def _reader_url(url: str) -> str:
    return f"{_READER_BASE_URL}/{url}"


def _title_from_markdown(markdown: str, url: str) -> str:
    match = _TITLE_RE.search(markdown)
    if match:
        return match.group(1).strip()
    return url


def _cooldown_state() -> tuple[float, str]:
    """Return remaining cooldown seconds and its redacted reason."""
    with _health_lock:
        remaining = max(0.0, _cooldown_until - time.monotonic())
        return remaining, _cooldown_reason


def _record_health_success() -> None:
    with _health_lock:
        for kind in _failure_streaks:
            _failure_streaks[kind] = 0


def _record_health_failure(
    reason: str, *, kind: str, immediate: bool = False
) -> None:
    global _cooldown_until, _cooldown_reason
    with _health_lock:
        if kind in _failure_streaks:
            for other_kind in _failure_streaks:
                if other_kind != kind:
                    _failure_streaks[other_kind] = 0
            _failure_streaks[kind] += 1
        if (
            immediate
            or _failure_streaks.get(kind, 0) >= _TRANSIENT_FAILURE_THRESHOLD
        ):
            _cooldown_until = time.monotonic() + _COOLDOWN_SECONDS
            _cooldown_reason = _redact(reason)
            for failure_kind in _failure_streaks:
                _failure_streaks[failure_kind] = 0


def _cooldown_result(url: str) -> Dict[str, Any]:
    remaining, reason = _cooldown_state()
    return {
        "url": url,
        "title": "",
        "content": "",
        "raw_content": "",
        "error": (
            f"Jina Reader is in cooldown for {remaining:.0f}s"
            + (f" after {reason}" if reason else "")
        ),
    }


def _reset_health_for_tests() -> None:
    """Reset process-local health state. Test-only."""
    global _cooldown_until, _cooldown_reason
    with _health_lock:
        for kind in _failure_streaks:
            _failure_streaks[kind] = 0
        _cooldown_until = 0.0
        _cooldown_reason = ""


class JinaReaderWebSearchProvider(WebSearchProvider):
    """Extract clean markdown via Jina Reader."""

    @property
    def name(self) -> str:
        return "jina"

    @property
    def display_name(self) -> str:
        return "Jina Reader"

    def is_available(self) -> bool:
        """Jina Reader has a no-key public path; an API key only raises limits."""
        remaining, _ = _cooldown_state()
        return remaining <= 0

    def supports_search(self) -> bool:
        return False

    def supports_extract(self) -> bool:
        return True

    def extract(self, urls: List[str], **kwargs: Any) -> List[Dict[str, Any]]:
        """Extract one or more URLs as markdown.

        Returns one legacy-shaped result per URL. Provider-level failures are
        represented as per-URL ``error`` entries so the dispatcher can advance
        to the next extract backend.
        """
        import httpx

        headers = {
            "Accept": "text/markdown, text/plain;q=0.9, */*;q=0.1",
            "X-Return-Format": "markdown",
        }
        api_key = get_provider_env("JINA_API_KEY")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        results: List[Dict[str, Any]] = []
        timeout = httpx.Timeout(10.0, connect=3.0, read=10.0)

        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            for url in urls:
                remaining, _ = _cooldown_state()
                if remaining > 0:
                    results.append(_cooldown_result(url))
                    continue
                try:
                    response = client.get(_reader_url(url), headers=headers)
                    response.raise_for_status()
                    content = response.text.strip()
                    if not content:
                        results.append(
                            {
                                "url": url,
                                "title": "",
                                "content": "",
                                "raw_content": "",
                                "error": "Jina Reader returned no content",
                            }
                        )
                        continue

                    _record_health_success()
                    title = _title_from_markdown(content, url)
                    results.append(
                        {
                            "url": url,
                            "title": title,
                            "content": content,
                            "raw_content": content,
                            "metadata": {
                                "sourceURL": url,
                                "title": title,
                                "provider": self.name,
                            },
                        }
                    )
                except httpx.HTTPStatusError as exc:
                    status = exc.response.status_code
                    if status in (401, 403, 429):
                        _record_health_failure(
                            f"HTTP {status}",
                            kind="rate_limit" if status == 429 else "auth",
                            immediate=status in (401, 403),
                        )
                    logger.warning("Jina Reader HTTP error for %s: %s", url, status)
                    results.append(
                        {
                            "url": url,
                            "title": "",
                            "content": "",
                            "raw_content": "",
                            "error": f"Jina Reader returned HTTP {status}",
                        }
                    )
                except httpx.RequestError as exc:
                    _record_health_failure(
                        f"transport failure: {_redact(exc)}",
                        kind="transport",
                    )
                    logger.warning("Jina Reader request error for %s: %s", url, exc)
                    results.append(
                        {
                            "url": url,
                            "title": "",
                            "content": "",
                            "raw_content": "",
                            "error": f"Jina Reader request failed: {_redact(exc)}",
                        }
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Jina Reader extract error for %s: %s", url, exc)
                    results.append(
                        {
                            "url": url,
                            "title": "",
                            "content": "",
                            "raw_content": "",
                            "error": f"Jina Reader extract failed: {_redact(exc)}",
                        }
                    )

        return results

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "Jina Reader",
            "badge": "free · no key",
            "tag": "Extracts readable markdown via r.jina.ai; JINA_API_KEY is optional for higher limits.",
            "env_vars": [
                {
                    "key": "JINA_API_KEY",
                    "prompt": "Optional Jina API key for higher Reader limits",
                    "url": "https://jina.ai/reader/",
                    "optional": True,
                },
            ],
        }
