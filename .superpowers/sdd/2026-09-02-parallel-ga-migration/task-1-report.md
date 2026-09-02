# Task 1 report — Parallel GA dependency state

## Scope

Owned repository files:

- `pyproject.toml`
- `tools/lazy_deps.py`
- `uv.lock`

No provider, documentation, configuration-default, or test file was edited.

Commit: `0585b67281` (`build: pin Parallel GA SDK`)

## Changes

- Replaced both obsolete `parallel-web==0.4.2` declarations with the bounded
  `parallel-web>=1.3.3,<2` contract.
- Regenerated `uv.lock`; it resolves `parallel-web==1.3.3` with updated wheel
  and source hashes.
- Added a package-specific `exclude-newer` cutoff. Independent review found
  the initial `2026-09-02T00:30:00Z` value broader than necessary. Follow-up
  commit `d35ff0901e` tightens it to `2026-09-01T00:26:00Z`, one second after the
  latest 1.3.3 artifact upload (`2026-09-01T00:25:58.265Z`), and documents
  removal after 2026-09-15 when the release has aged through the global
  14-day policy.

## Verification

```text
uv lock
Resolved 249 packages
Updated parallel-web v0.4.2 -> v1.3.3

uv sync --extra parallel-web
Installed parallel-web==1.3.3

uv run python -c 'from importlib.metadata import version; from parallel import Parallel, AsyncParallel; ...'
1.3.3
Parallel AsyncParallel
parallel-ga-import-ok

uv lock --check
Resolved 249 packages

scripts/run_tests.sh tests/plugins/web/test_web_search_provider_plugins.py tests/tools/test_web_tools_config.py tests/tools/test_web_provider_query_logging.py
66 passed, 0 failed

scripts/run_tests.sh tests/test_packaging_metadata.py tests/test_project_metadata.py
14 passed, 0 failed

git diff --check
clean
```

The requested third baseline path,
`tests/tools/test_web_provider_query_logging.py`, does not exist at this base;
the repository test runner discovered and ran the other two unchanged files.
The worktree environment required the existing `dev` extra before the test
runner could select it instead of a fallback interpreter; this changed only
the untracked virtual environment.

## Safety and scope review

- No API key or other credential was read or printed.
- No Parallel client was constructed and no paid/network API call was made.
- The repository diff is limited to the three owned files.
- The live checkout's two dirty overlapping test files were not edited.

## Review follow-up

- Finding: the original package cutoff was permanent configuration and used a
  timestamp broader than the minimum needed for 1.3.3.
- Resolution: tightened the cutoff to `2026-09-01T00:26:00Z` and added an
  explicit removal date after 2026-09-15. The timestamp does not expire by
  itself; the dated comment makes removal a deliberate maintenance action.
- Follow-up commit: `d35ff0901e` (`build: tighten Parallel GA cutoff`).
- Follow-up verification: `uv lock --check` passed; the real local imports of
  `Parallel` and `AsyncParallel` resolved `parallel-web==1.3.3`; the packaging,
  project-metadata, and three lazy-dependency files passed with 93 tests, 0
  failures, and 2 expected Windows-only skips; `git diff --check` passed.
