"""Local rendered browser extract plugin — bundled, auto-loaded."""

from __future__ import annotations

from plugins.web.local_browser.provider import LocalBrowserWebSearchProvider


def register(ctx) -> None:
    """Register the local browser provider with the plugin context."""
    ctx.register_web_search_provider(LocalBrowserWebSearchProvider())
