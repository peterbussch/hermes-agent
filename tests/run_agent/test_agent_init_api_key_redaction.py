"""Behavior tests for startup credential output."""

from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.parametrize(
    ("api_mode", "provider", "base_url"),
    [
        ("chat_completions", "openrouter", "https://openrouter.ai/api/v1"),
        ("anthropic_messages", "anthropic", "https://api.anthropic.com"),
    ],
)
def test_agent_startup_does_not_print_api_key_fragments(
    api_mode, provider, base_url, capsys
):
    """Startup output may report configuration, but never key material."""
    from run_agent import AIAgent

    secret = "sk-startup-redaction-sentinel-0123456789"
    with ExitStack() as stack:
        stack.enter_context(patch("run_agent.get_tool_definitions", return_value=[]))
        stack.enter_context(patch("run_agent.check_toolset_requirements", return_value={}))
        stack.enter_context(patch("run_agent.OpenAI"))
        if api_mode == "anthropic_messages":
            stack.enter_context(
                patch(
                    "agent.anthropic_adapter.build_anthropic_client",
                    return_value=MagicMock(),
                )
            )

        AIAgent(
            api_key=secret,
            base_url=base_url,
            provider=provider,
            api_mode=api_mode,
            model="claude-sonnet-4-6",
            quiet_mode=False,
            skip_context_files=True,
            skip_memory=True,
        )

    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert secret not in output
    assert secret[:8] not in output
    assert secret[-4:] not in output
    assert "API key configured" in output
