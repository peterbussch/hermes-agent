"""Single-query chat cannot receive detached tool completions after exit."""

from __future__ import annotations

import sys
import types


def test_chat_query_declares_stateless_delivery_before_agent_startup(monkeypatch):
    import hermes_cli.main as main_mod
    from gateway.session_context import async_delivery_supported, reset_session_vars
    from hermes_cli._parser import build_top_level_parser

    parser, _subparsers, chat_parser = build_top_level_parser()
    chat_parser.set_defaults(func=main_mod.cmd_chat)
    args = parser.parse_args(["chat", "-q", "dispatch a child"])
    observed: dict[str, bool] = {}
    fake_cli = types.ModuleType("cli")

    def fake_main(**_kwargs):
        observed["async_delivery_supported"] = async_delivery_supported()

    monkeypatch.setattr(main_mod, "_has_any_provider_configured", lambda: True)
    monkeypatch.setattr(main_mod, "_pin_kanban_board_env", lambda: None)
    monkeypatch.setattr(main_mod, "_sync_bundled_skills_for_startup", lambda: None)
    monkeypatch.setattr(main_mod, "_termux_should_prefetch_update_check", lambda: False)
    fake_cli.main = fake_main
    monkeypatch.setitem(sys.modules, "cli", fake_cli)

    reset_session_vars()
    try:
        main_mod.cmd_chat(args)
    finally:
        reset_session_vars()

    assert observed == {"async_delivery_supported": False}
