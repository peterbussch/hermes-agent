"""Tests for warn_deprecated_cwd_env_vars() migration warning."""


class TestDeprecatedCwdWarning:
    """Warn when MESSAGING_CWD or TERMINAL_CWD is set in .env."""

    def test_messaging_cwd_triggers_warning(self, monkeypatch, capsys):
        from hermes_cli import config

        monkeypatch.setattr(config, "load_env", lambda: {"MESSAGING_CWD": "/some/path"})
        monkeypatch.delenv("MESSAGING_CWD", raising=False)
        monkeypatch.delenv("TERMINAL_CWD", raising=False)

        config.warn_deprecated_cwd_env_vars(config={})

        captured = capsys.readouterr()
        assert "MESSAGING_CWD" in captured.err
        assert "deprecated" in captured.err.lower()
        assert "config.yaml" in captured.err


    def test_both_deprecated_vars_warn(self, monkeypatch, capsys):
        from hermes_cli import config

        monkeypatch.setattr(
            config,
            "load_env",
            lambda: {"MESSAGING_CWD": "/msg/path", "TERMINAL_CWD": "/term/path"},
        )
        monkeypatch.delenv("MESSAGING_CWD", raising=False)
        monkeypatch.delenv("TERMINAL_CWD", raising=False)

        config.warn_deprecated_cwd_env_vars(config={})

        captured = capsys.readouterr()
        assert "MESSAGING_CWD" in captured.err
        assert "TERMINAL_CWD" in captured.err

    def test_runtime_bridge_env_does_not_trigger_false_warning(self, monkeypatch, capsys):
        from hermes_cli import config

        monkeypatch.setattr(config, "load_env", lambda: {})
        monkeypatch.setenv("TERMINAL_CWD", "/runtime/bridge/path")

        config.warn_deprecated_cwd_env_vars(config={"terminal": {"cwd": "."}})

        captured = capsys.readouterr()
        assert captured.err == ""
