"""Conservative semantic quality classification for extracted web pages.

The extractor transport succeeding does not mean it returned useful source
material.  Browser interstitials, login walls, and JavaScript placeholders
often arrive as HTTP-200 text.  This module gives providers and the dispatcher
one small, deterministic vocabulary for those outcomes.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Mapping
from urllib.parse import urlsplit


_CHALLENGE_RE = re.compile(
    r"(?:checking your browser|verify (?:that )?you are human|captcha|"
    r"cloudflare ray id|just a moment|security challenge|"
    r"unusual traffic from your computer|web application firewall|"
    r"request (?:was )?rejected by the server|requested url was rejected|"
    r"access denied[\s\S]{0,1000}?reference|"
    r"access to this page has been denied)",
    re.IGNORECASE,
)
_LOGIN_RE = re.compile(
    r"(?:sign in to continue|log in to continue|please (?:sign|log) in|"
    r"authentication required|create an account to continue|"
    r"subscribers? only)",
    re.IGNORECASE,
)
_PLACEHOLDER_RE = re.compile(
    r"(?:enable javascript|javascript (?:is )?disabled|"
    r"content (?:is )?(?:unavailable|not available)|"
    r"unsupported browser|this page (?:isn't|is not) available|"
    r"please wait while we redirect)",
    re.IGNORECASE,
)


def classify_extraction(result: Mapping[str, Any]) -> Dict[str, Any]:
    """Return quality fields for a legacy-shaped extraction result.

    The classifier is deliberately conservative: short text is ``partial``,
    not automatically a placeholder, unless a known interstitial signature is
    present.  Callers can safely retry anything except ``article`` while still
    retaining the best partial attempt if every provider is degraded.
    """
    content = str(result.get("raw_content") or result.get("content") or "").strip()
    title = str(result.get("title") or "").strip()
    error = str(result.get("error") or "").strip()
    sample = f"{title}\n{content}"[:20_000]
    hostname = (urlsplit(str(result.get("url") or "")).hostname or "").lower()
    host_label = hostname.removeprefix("www.")
    normalized_content = re.sub(r"[^a-z0-9.-]+", "", content.lower()).strip(".")

    if not content:
        return {
            "content_class": "empty",
            "status": "error",
            "completeness": 0.0,
            "blocker": error or "empty_content",
        }

    if _CHALLENGE_RE.search(sample):
        return {
            "content_class": "challenge",
            "status": "blocked",
            "completeness": 0.0,
            "blocker": "anti_bot_challenge",
        }
    if _LOGIN_RE.search(sample):
        return {
            "content_class": "login_wall",
            "status": "blocked",
            "completeness": 0.0,
            "blocker": "authentication_required",
        }
    if _PLACEHOLDER_RE.search(sample):
        return {
            "content_class": "placeholder",
            "status": "blocked",
            "completeness": 0.0,
            "blocker": "placeholder_response",
        }
    if host_label and normalized_content in {hostname.strip("."), host_label.strip(".")}:
        return {
            "content_class": "placeholder",
            "status": "blocked",
            "completeness": 0.0,
            "blocker": "domain_placeholder",
        }

    if error:
        return {
            "content_class": "partial",
            "status": "partial",
            "completeness": round(min(0.55, max(0.1, len(content) / 320.0)), 2),
            "blocker": error,
        }

    word_count = len(re.findall(r"\b[\w'-]+\b", content))
    if len(content) < 160 or word_count < 20:
        completeness = round(min(0.55, max(0.1, len(content) / 320.0)), 2)
        return {
            "content_class": "partial",
            "status": "partial",
            "completeness": completeness,
            "blocker": error or "insufficient_content",
        }

    completeness = round(min(1.0, 0.6 + (len(content) / 5_000.0)), 2)
    return {
        "content_class": "article",
        "status": "success",
        "completeness": completeness,
        "blocker": None,
    }


def annotate_extraction(result: Mapping[str, Any]) -> Dict[str, Any]:
    """Copy *result* and attach the canonical semantic quality fields."""
    annotated = dict(result)
    annotated.update(classify_extraction(result))
    return annotated
