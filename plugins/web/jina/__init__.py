"""Jina Reader extract plugin — bundled, auto-loaded."""

from __future__ import annotations

from plugins.web.jina.provider import JinaReaderWebSearchProvider


def register(ctx) -> None:
    """Register the Jina Reader provider with the plugin context."""
    ctx.register_web_search_provider(JinaReaderWebSearchProvider())
