#!/usr/bin/env python3
"""Rendered-page extraction with semantic quality and stable exit status.

This is the repository-owned implementation behind
``$HERMES_HOME/bin/agentic-browser-extract``. JSON output defaults to a
self-describing envelope; ``--legacy-list`` preserves the historical bare
result list for callers that have not migrated yet.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import uuid
from typing import Any

from plugins.web.local_browser.content_quality import annotate_extraction
from plugins.web.local_browser.provider import (
    LocalBrowserWebSearchProvider,
    _runtime_deps_available,
    find_chromium_executable,
)
from tools.url_safety import async_is_safe_url
from tools.website_policy import check_website_access


def _status() -> dict[str, Any]:
    chromium = find_chromium_executable()
    provider = LocalBrowserWebSearchProvider()
    return {
        "ok": bool(provider.is_available()),
        "provider": provider.name,
        "runtime_deps": bool(_runtime_deps_available()),
        "chromium": str(chromium) if chromium else "",
    }


def _trim_result(result: dict[str, Any], limit_chars: int) -> dict[str, Any]:
    if limit_chars <= 0:
        return result
    trimmed = dict(result)
    for key in ("content", "raw_content"):
        value = trimmed.get(key)
        if isinstance(value, str) and len(value) > limit_chars:
            trimmed[key] = value[:limit_chars] + "\n...[truncated]"
    return trimmed


async def _extract(urls: list[str], limit_chars: int) -> list[dict[str, Any]]:
    provider = LocalBrowserWebSearchProvider()
    results: list[dict[str, Any]] = []
    for url in urls:
        blocked = check_website_access(url)
        if blocked:
            result = {
                "url": url,
                "title": "",
                "content": "",
                "raw_content": "",
                "error": blocked["message"],
                "blocked_by_policy": {
                    "host": blocked["host"],
                    "rule": blocked["rule"],
                    "source": blocked["source"],
                },
            }
        elif not await async_is_safe_url(url):
            result = {
                "url": url,
                "title": "",
                "content": "",
                "raw_content": "",
                "error": (
                    "Blocked: URL targets a private or internal network address"
                ),
            }
        else:
            try:
                raw = await provider.extract([url])
                if len(raw) != 1 or not isinstance(raw[0], dict):
                    raise ValueError("Local browser returned no result for this URL")
                result = dict(raw[0])
                returned_url = str(result.get("url") or "").strip()
                if returned_url and returned_url != url:
                    raise ValueError(
                        "Local browser returned no result for this URL "
                        f"(unexpected URL {returned_url})"
                    )
                result["url"] = url
            except Exception as exc:  # noqa: BLE001 — stable operator envelope
                result = {
                    "url": url,
                    "title": "",
                    "content": "",
                    "raw_content": "",
                    "error": f"Local browser extract failed: {exc}",
                }
        results.append(annotate_extraction(result))
    return results


def _quality_score(result: dict[str, Any]) -> float:
    class_score = {
        "article": 10.0,
        "partial": 5.0,
        "placeholder": 1.0,
        "login_wall": 1.0,
        "challenge": 1.0,
        "empty": 0.0,
    }.get(str(result.get("content_class")), 0.0)
    return class_score + float(result.get("completeness") or 0.0)


def _needs_browser_tool_fallback(result: dict[str, Any]) -> bool:
    return annotate_extraction(result).get("content_class") != "article"


def _browser_tool_extract_one(url: str, limit_chars: int) -> dict[str, Any]:
    """Use Hermes' built-in browser stack as the bounded second renderer."""
    task_id = f"agentic-browser-extract-{uuid.uuid4().hex[:12]}"
    try:
        from tools.browser_tool import browser_console, browser_navigate
        from tools.browser_tool_lifecycle import cleanup_browser

        nav = json.loads(browser_navigate(url, task_id=task_id))
        if not nav.get("success"):
            result = {
                "url": url,
                "title": "",
                "content": "",
                "raw_content": "",
                "error": nav.get("error") or "browser_navigate failed",
            }
        else:
            text_result = json.loads(
                browser_console(
                    expression="document.body ? document.body.innerText : ''",
                    task_id=task_id,
                )
            )
            if not text_result.get("success"):
                result = {
                    "url": nav.get("url") or url,
                    "title": nav.get("title") or "",
                    "content": "",
                    "raw_content": "",
                    "error": (
                        text_result.get("error")
                        or "browser_console extraction failed"
                    ),
                }
            else:
                content = str(text_result.get("result") or "").strip()
                result = {
                    "url": nav.get("url") or url,
                    "title": nav.get("title") or url,
                    "content": content,
                    "raw_content": content,
                    "metadata": {
                        "provider": "browser_tool",
                        "fallback_from": "local_browser",
                        "element_count": nav.get("element_count"),
                    },
                }
                if not content:
                    result["error"] = "Browser tool rendered no extractable text"
        annotated = annotate_extraction(result)
        annotated["provider"] = "browser_tool"
        return annotated
    except Exception as exc:  # noqa: BLE001
        return annotate_extraction(
            {
                "url": url,
                "title": "",
                "content": "",
                "raw_content": "",
                "error": (
                    f"Browser tool fallback failed: {type(exc).__name__}: {exc}"
                ),
                "provider": "browser_tool",
            }
        )
    finally:
        try:
            cleanup_browser(task_id=task_id)  # type: ignore[name-defined]
        except Exception:
            pass


def _apply_browser_tool_fallback(
    urls: list[str],
    results: list[dict[str, Any]],
    limit_chars: int,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for index, url in enumerate(urls):
        raw_result = (
            results[index]
            if index < len(results) and isinstance(results[index], dict)
            else {
                "url": url,
                "title": "",
                "content": "",
                "raw_content": "",
                "error": "Local browser returned no result for this URL",
            }
        )
        result = annotate_extraction(raw_result)
        result.setdefault("provider", "local_browser")
        if not _needs_browser_tool_fallback(result):
            result["fallback_attempted"] = False
            output.append(result)
            continue

        fallback = _browser_tool_extract_one(url, limit_chars)
        selected = fallback if _quality_score(fallback) > _quality_score(result) else result
        selected = dict(selected)
        selected["fallback_attempted"] = True
        selected["fallback_from"] = "local_browser"
        selected["fallback_result"] = {
            "provider": fallback.get("provider", "browser_tool"),
            "status": fallback.get("status"),
            "content_class": fallback.get("content_class"),
            "completeness": fallback.get("completeness"),
            "blocker": fallback.get("blocker"),
            "error": fallback.get("error"),
        }
        output.append(selected)
    return output


def _markdown(results: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for result in results:
        title = result.get("title") or result.get("url") or "Untitled"
        parts.extend(
            [
                f"# {title}",
                "",
                f"Source: {result.get('url', '')}",
                f"Status: {result.get('status')} ({result.get('content_class')})",
            ]
        )
        if result.get("blocker"):
            parts.append(f"Blocker: {result['blocker']}")
        content = str(result.get("content") or "").strip()
        if content:
            parts.extend(["", content])
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render URLs and reject placeholders/challenge pages."
    )
    parser.add_argument("urls", nargs="*", help="URL(s) to render and extract")
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    parser.add_argument("--limit-chars", type=int, default=20_000)
    parser.add_argument("--wait", type=float)
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--no-browser-tool-fallback", action="store_true")
    parser.add_argument(
        "--legacy-list",
        action="store_true",
        help="Emit the historical bare JSON result list",
    )
    args = parser.parse_args(argv)

    if args.wait is not None:
        os.environ["LOCAL_BROWSER_WAIT_SECONDS"] = str(args.wait)
    os.environ["LOCAL_BROWSER_TIMEOUT_SECONDS"] = str(args.timeout)

    if args.status or args.self_test:
        readiness = _status()
        print(json.dumps(readiness, indent=2, sort_keys=True))
        return 0 if readiness.get("ok") or not args.self_test else 1
    if not args.urls:
        parser.error("provide at least one URL, or use --status/--self-test")

    initial_raw = asyncio.run(_extract(args.urls, args.limit_chars))
    initial_results: list[dict[str, Any]] = []
    for index, url in enumerate(args.urls):
        if index < len(initial_raw) and isinstance(initial_raw[index], dict):
            result = dict(initial_raw[index])
            result["url"] = url
        else:
            result = {
                "url": url,
                "title": "",
                "content": "",
                "raw_content": "",
                "error": "Local browser returned no result for this URL",
            }
        initial_results.append(annotate_extraction(result))
    fallback_needed = [
        result.get("content_class") != "article" for result in initial_results
    ]
    results = initial_results
    if not args.no_browser_tool_fallback:
        results = _apply_browser_tool_fallback(
            args.urls, results, args.limit_chars
        )
    results = [annotate_extraction(result) for result in results]
    for index, result in enumerate(results):
        result.setdefault(
            "provider",
            str((result.get("metadata") or {}).get("provider") or "local_browser"),
        )
        result.setdefault(
            "fallback_attempted",
            bool(fallback_needed[index] and not args.no_browser_tool_fallback),
        )

    ok = (
        bool(results)
        and len(results) == len(args.urls)
        and all(result.get("content_class") == "article" for result in results)
    )
    serialized_results = [
        _trim_result(result, args.limit_chars) for result in results
    ]
    readiness = _status()
    if args.format == "markdown":
        print(_markdown(serialized_results), end="")
    elif args.legacy_list:
        print(json.dumps(serialized_results, indent=2, sort_keys=True))
    else:
        print(
            json.dumps(
                {
                    "ok": ok,
                    "results": serialized_results,
                    "readiness": readiness,
                },
                indent=2,
                sort_keys=True,
            )
        )
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
