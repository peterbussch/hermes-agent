"""Real-git regression tests for the fork upstream sync in ``hermes update``.

The bug these pin: divergence was measured off ``origin/main`` — the fork's GitHub mirror — while
``git pull``/``git merge`` move the LOCAL branch. A mirror left stale before the local commits
existed reports zero divergence, so the updater ran an ff-only pull that could never succeed against
diverging history, printed "✓ Up to date with your fork", and exited 0 with the checkout thousands
of commits behind.

Both tests build the real topology with real git (bare repos on disk, no network, no mocks) because
the contract is about what git actually does to the tree, not about which command was issued.
"""

import subprocess
from pathlib import Path

import pytest

from hermes_cli import update_cmd_git as gitmod

# The fixed module exposes UPSTREAM_STATE_CURRENT; on the unfixed base the sync returns a bool, so
# read it defensively. Either way the assertion means "the sync reported the checkout as current",
# which is what the unfixed code claimed while it had in fact applied nothing.
_CURRENT = getattr(gitmod, "UPSTREAM_STATE_CURRENT", True)


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=check, capture_output=True, text=True)


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "--quiet")
    _git(path, "symbolic-ref", "HEAD", "refs/heads/main")
    _configure_identity(path)


def _configure_identity(path: Path) -> None:
    """Commit identity belongs to the repo under test, never to the runner's global config."""
    _git(path, "config", "user.email", "upstream-sync@example.invalid")
    _git(path, "config", "user.name", "Upstream Sync Test")


def _commit(path: Path, message: str) -> str:
    _git(path, "add", "-A")
    _git(path, "commit", "--quiet", "-m", message)
    return _git(path, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture()
def diverged_fork(tmp_path):
    """A fork whose local branch has its own commit AND trails upstream, with a stale origin mirror.

    Returns ``(checkout, upstream_bare, seed, local_sha)``. Upstream gains a commit the fork has
    never seen, and ``origin`` (the fork's mirror) is deliberately left at the pre-divergence commit
    — the shape that made the divergence read as zero.
    """
    upstream_bare = tmp_path / "upstream.git"
    upstream_bare.mkdir()
    _git(upstream_bare, "init", "--quiet", "--bare")

    seed = tmp_path / "seed"
    _init_repo(seed)
    (seed / "base.txt").write_text("base\n")
    _commit(seed, "base")
    _git(seed, "remote", "add", "origin", str(upstream_bare))
    _git(seed, "push", "--quiet", "-u", "origin", "main")
    # A bare repo's HEAD is unborn until told; without this the clone checks out nothing.
    _git(upstream_bare, "symbolic-ref", "HEAD", "refs/heads/main")

    checkout = tmp_path / "checkout"
    subprocess.run(
        ["git", "clone", "--quiet", str(upstream_bare), str(checkout)],
        cwd=tmp_path, check=True, capture_output=True, text=True)
    _configure_identity(checkout)
    _git(checkout, "remote", "add", "upstream", str(upstream_bare))

    # The fork's own work, never pushed to its mirror.
    (checkout / "local.txt").write_text("local\n")
    local_sha = _commit(checkout, "local work")

    # Upstream moves on while the mirror stays where it was.
    (seed / "upstream.txt").write_text("upstream\n")
    _commit(seed, "upstream work")
    _git(seed, "push", "--quiet", "origin", "main")
    return checkout, upstream_bare, seed, local_sha


def test_sync_applies_upstream_and_preserves_local_commits(diverged_fork, capsys):
    """A fork that is behind upstream ends up containing upstream's work, with its own commits intact.

    Proven red on the unfixed base: the ff-only pull aborted on the diverging branch, so
    ``upstream.txt`` never arrived while the run still claimed to be up to date.
    """
    checkout, upstream_bare, _seed, local_sha = diverged_fork
    upstream_tip = _git(upstream_bare, "rev-parse", "main").stdout.strip()

    assert gitmod._sync_with_upstream_if_needed(["git"], checkout) == _CURRENT

    capsys.readouterr()
    assert (checkout / "upstream.txt").exists(), "upstream's work was never applied"
    assert (checkout / "local.txt").read_text() == "local\n", "local work was clobbered"
    for sha, what in ((upstream_tip, "upstream"), (local_sha, "local")):
        assert _git(checkout, "merge-base", "--is-ancestor", sha, "HEAD", check=False).returncode == 0, (
            f"{what} history is not an ancestor of HEAD")
    # A real merge, so both histories survive — not a clobber and not a fast-forward.
    assert len(_git(checkout, "rev-list", "--parents", "-n", "1", "HEAD").stdout.split()) == 3


def test_conflicted_sync_leaves_checkout_untouched_and_says_so(diverged_fork, capsys):
    """When upstream cannot be applied, the tree is left exactly as found and the run says so.

    The "says so" half is the regression: this notice is what the caller turns into a "partial"
    update instead of "✓ Up to date". Proven red on the unfixed base, which printed only
    "✗ Failed to pull from upstream" and still exited 0.
    """
    checkout, _upstream_bare, seed, _local_sha = diverged_fork
    # Same file, both sides: the merge cannot be resolved without a human.
    (checkout / "base.txt").write_text("local edit\n")
    local_sha = _commit(checkout, "local edits base.txt")
    (seed / "base.txt").write_text("upstream edit\n")
    _commit(seed, "upstream edits base.txt")
    _git(seed, "push", "--quiet", "origin", "main")

    state = gitmod._sync_with_upstream_if_needed(["git"], checkout)
    out = capsys.readouterr().out

    assert state != _CURRENT, "a conflicted sync must not be reported as current"
    # The notice must state the real remaining gap rather than a frozen number.
    behind = _git(checkout, "rev-list", "--count", "HEAD..upstream/main").stdout.strip()
    assert int(behind) > 0
    assert f"still {behind} commit(s) behind" in out
    assert "base.txt" in out, "the conflicting path was not named"
    assert _git(checkout, "rev-parse", "HEAD").stdout.strip() == local_sha, "HEAD moved on a conflict"
    assert _git(checkout, "status", "--porcelain").stdout == "", "checkout left dirty"
    assert not (checkout / ".git" / "MERGE_HEAD").exists(), "left mid-merge"
    assert (checkout / "base.txt").read_text() == "local edit\n"
