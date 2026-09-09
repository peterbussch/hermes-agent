"""The worktree test runner must use the canonical installation, not retired state."""
import os
from pathlib import Path
import shutil
import subprocess


def test_worktree_runner_finds_canonical_venv_without_legacy_checkout(tmp_path):
    source = Path(__file__).resolve().parents[1] / 'scripts/run_tests.sh'
    worktree = tmp_path / 'worktree'
    (worktree / 'scripts').mkdir(parents=True)
    runner = worktree / 'scripts/run_tests.sh'
    shutil.copy2(source, runner)
    home = tmp_path / 'home'
    binary = home / 'Code/hermes-agent/venv/bin'
    binary.mkdir(parents=True)
    (binary / 'activate').touch()
    python = binary / 'python'
    python.write_text('#!/bin/sh\nprintf "CANONICAL_PYTHON:%s\\n" "$*"\n')
    python.chmod(0o700)
    result = subprocess.run(
        ['bash', str(runner), 'tests/no-live-test.py'], cwd=worktree,
        env={'PATH': '/usr/bin:/bin', 'HOME': str(home)},
        text=True, capture_output=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'CANONICAL_PYTHON:' in result.stdout
    assert 'run_tests_parallel.py' in result.stdout
    assert not os.path.lexists(home / '.hermes/hermes-agent')
