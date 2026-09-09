"""Thinking control for local Ollama models routed via OmniRoute (``local-*``).

gemma4's Ollama renderer keeps thinking ON by default. Through OmniRoute that
makes the agentic worker stream only reasoning — ``_strip_think_blocks()`` then
leaves empty content, short/non-streaming calls hit OmniRoute's ~24s
empty-content reset, and dispatches die as "truncated after 3 continuation
attempts". So local models default to thinking OFF for deterministic worker
output, and are adjustable PER DISPATCH via ``HERMES_REASONING_EFFORT``.

This is a leaf module (no agent imports) so every request-building path —
main transport, streaming/non-streaming chokepoints, iteration-limit summary,
and auxiliary clients — can apply it without circular-import risk.
"""
from __future__ import annotations

import os

_VALID = ("low", "medium", "high")


def local_reasoning_effort() -> str:
    """Return the reasoning_effort to use for a local model.

    ``HERMES_REASONING_EFFORT=low|medium|high`` turns thinking on at that level;
    anything else (including unset / "none" / "off") → ``"none"`` (thinking off).
    Independent of the session-global reasoning_config so local stays off unless
    the caller explicitly opts in for that dispatch.
    """
    effort = (os.getenv("HERMES_REASONING_EFFORT") or "").strip().lower()
    return effort if effort in _VALID else "none"


def is_local_model(model: object) -> bool:
    return isinstance(model, str) and model.startswith("local-")


def apply_local_reasoning(kwargs: dict) -> dict:
    """If kwargs targets a local model, pin reasoning_effort (unless already set).

    Mutates and returns ``kwargs``. Safe to call on any request kwargs dict at
    any chokepoint; a no-op for non-local models.
    """
    if not isinstance(kwargs, dict):
        return kwargs
    if is_local_model(kwargs.get("model")) and "reasoning_effort" not in kwargs:
        kwargs["reasoning_effort"] = local_reasoning_effort()
    return kwargs
