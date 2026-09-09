"""Local rendered browser extraction via nodriver + trafilatura.

This extract-only provider drives the local Playwright-managed Chromium through
CDP, waits briefly for JavaScript-rendered content, then converts the rendered
HTML to markdown with trafilatura. It is the local fallback behind Jina Reader
for pages that need a real browser.
"""

from __future__ import annotations

import asyncio
import html
import importlib.util
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.redact import redact_sensitive_text
from agent.web_search_provider import WebSearchProvider
from plugins.web.local_browser.content_quality import annotate_extraction

logger = logging.getLogger(__name__)


_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style|noscript)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL
)
_TAG_RE = re.compile(r"<[^>]+>")
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_BROWSER_EXIT_TIMEOUT_SECONDS = 5.0


def _redact(error: Any) -> str:
    return redact_sensitive_text(str(error), force=True)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _has_module(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def _runtime_deps_available() -> bool:
    return _has_module("nodriver") and _has_module("trafilatura")


def _ensure_runtime_deps() -> Optional[str]:
    """Ensure optional browser-render extraction deps are installed."""
    if _runtime_deps_available():
        return None

    try:
        from tools.lazy_deps import ensure as _lazy_ensure

        _lazy_ensure("search.local_browser", prompt=False)
    except ImportError:
        return (
            "Local browser extract requires nodriver==0.50.3 and "
            "trafilatura==2.1.0"
        )
    except Exception as exc:  # noqa: BLE001 — lazy_deps surfaces install hints
        return f"Local browser dependencies unavailable: {_redact(exc)}"

    if not _runtime_deps_available():
        return (
            "Local browser dependencies installed but are not importable; "
            "restart Hermes and try again"
        )
    return None


def _title_from_html(rendered_html: str, url: str) -> str:
    match = _TITLE_RE.search(rendered_html)
    if not match:
        return url
    title = html.unescape(match.group(1))
    return " ".join(title.split()) or url


def _fallback_text(rendered_html: str) -> str:
    body = _SCRIPT_STYLE_RE.sub("\n", rendered_html)
    body = _TAG_RE.sub("\n", body)
    body = html.unescape(body)
    lines = [" ".join(line.split()) for line in body.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _candidate_executables() -> List[Path]:
    home = Path.home()
    cache = home / "Library" / "Caches" / "ms-playwright"
    patterns = (
        "chromium-*/chrome-mac*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
        "chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium",
        "chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell",
    )

    candidates: List[Path] = []
    for pattern in patterns:
        group = [path for path in cache.glob(pattern) if path.is_file()]
        candidates.extend(
            sorted(group, key=lambda path: (path.stat().st_mtime, str(path)), reverse=True)
        )

    return candidates


def find_chromium_executable() -> Optional[Path]:
    """Return the configured or Playwright-managed Chromium executable."""
    override = os.getenv("LOCAL_BROWSER_EXECUTABLE", "").strip()
    if override:
        path = Path(override).expanduser()
        return path if path.is_file() else None

    candidates = _candidate_executables()
    return candidates[0] if candidates else None


class LocalBrowserWebSearchProvider(WebSearchProvider):
    """Render pages locally, then extract readable markdown."""

    @property
    def name(self) -> str:
        return "local_browser"

    @property
    def display_name(self) -> str:
        return "Local Browser"

    def is_available(self) -> bool:
        """Return True when nodriver, trafilatura, and Chromium are available."""
        return _runtime_deps_available() and find_chromium_executable() is not None

    def supports_search(self) -> bool:
        return False

    def supports_extract(self) -> bool:
        return True

    async def extract(self, urls: List[str], **kwargs: Any) -> List[Dict[str, Any]]:
        """Render and extract each URL with a hard per-URL timeout."""
        dep_error = _ensure_runtime_deps()
        if dep_error is not None:
            return [
                {
                    "url": url,
                    "title": "",
                    "content": "",
                    "raw_content": "",
                    "error": dep_error,
                }
                for url in urls
            ]

        executable = find_chromium_executable()
        if executable is None:
            return [
                {
                    "url": url,
                    "title": "",
                    "content": "",
                    "raw_content": "",
                    "error": "Local Chromium executable not found",
                }
                for url in urls
            ]

        timeout = _env_float("LOCAL_BROWSER_TIMEOUT_SECONDS", 20.0)
        results: List[Dict[str, Any]] = []

        for url in urls:
            try:
                result = await asyncio.wait_for(
                    self._extract_one(url, executable),
                    timeout=timeout,
                )
                results.append(result)
            except asyncio.TimeoutError:
                logger.warning("Local browser extract timed out for %s", url)
                results.append(
                    {
                        "url": url,
                        "title": "",
                        "content": "",
                        "raw_content": "",
                        "error": f"Local browser extract timed out after {timeout:g}s",
                    }
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Local browser extract failed for %s: %s", url, exc)
                results.append(
                    {
                        "url": url,
                        "title": "",
                        "content": "",
                        "raw_content": "",
                        "error": f"Local browser extract failed: {_redact(exc)}",
                    }
                )

        return [annotate_extraction(result) for result in results]

    async def _extract_one(self, url: str, executable: Path) -> Dict[str, Any]:
        import nodriver as uc
        import trafilatura

        browser = None
        profile_dir = tempfile.TemporaryDirectory(prefix="hermes-local-browser-")
        try:
            browser = await uc.start(
                headless=True,
                browser_executable_path=str(executable),
                user_data_dir=profile_dir.name,
                browser_args=[
                    "--disable-background-networking",
                    "--disable-dev-shm-usage",
                    "--disable-extensions",
                    "--disable-sync",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
                sandbox=True,
            )
            tab = await browser.get(url)
            await tab.sleep(_env_float("LOCAL_BROWSER_WAIT_SECONDS", 2.0))
            rendered_html = await tab.get_content()
            title = _title_from_html(rendered_html, url)
            content = trafilatura.extract(
                rendered_html,
                url=url,
                output_format="markdown",
                include_comments=False,
                include_tables=True,
            )
            if not content:
                content = _fallback_text(rendered_html)
            content = (content or "").strip()

            if not content:
                return {
                    "url": url,
                    "title": title,
                    "content": "",
                    "raw_content": "",
                    "error": "Local browser rendered no extractable content",
                }

            return {
                "url": url,
                "title": title,
                "content": content,
                "raw_content": content,
                "metadata": {
                    "sourceURL": url,
                    "title": title,
                    "provider": self.name,
                    "browser": str(executable),
                },
            }
        finally:
            if browser is not None:
                process = browser._process
                try:
                    await browser.aclose()
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Local browser CDP close failed: %s", _redact(exc))
                finally:
                    try:
                        browser.stop()
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "Local browser process termination failed: %s",
                            _redact(exc),
                        )
                    try:
                        await asyncio.wait_for(
                            process.wait(), timeout=_BROWSER_EXIT_TIMEOUT_SECONDS
                        )
                    except asyncio.TimeoutError:
                        logger.warning(
                            "Local browser process did not exit after %.1fs; killing it",
                            _BROWSER_EXIT_TIMEOUT_SECONDS,
                        )
                        try:
                            process.kill()
                        except ProcessLookupError:
                            pass
                        await process.wait()
            profile_dir.cleanup()

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "Local Browser",
            "badge": "free · local",
            "tag": "Renders JavaScript pages with local Chromium, then extracts markdown.",
            "env_vars": [
                {
                    "key": "LOCAL_BROWSER_EXECUTABLE",
                    "prompt": "Optional Chromium executable path",
                    "url": "",
                    "optional": True,
                },
            ],
        }
