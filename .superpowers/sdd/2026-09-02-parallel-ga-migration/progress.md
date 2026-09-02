# SDD ledger — Parallel GA migration

Plan: `/Users/peterbusscher/Code/hermes-harness/.worktrees/search-harness-completion/docs/superpowers/plans/2026-09-02-parallel-ga-migration.md`

Base: `d709d29f19975df16ee874c33266211e14987c78`
Branch: `search/parallel-ga-migration`
Worktree: `/Users/peterbusscher/Code/hermes-agent-worktrees/parallel-ga-migration`
Live source: `/Users/peterbusscher/Code/hermes-agent`

## Controller preflight

- The migration-owned provider, initializer, dependency declarations, lock,
  config defaults, and documentation are clean in the live source checkout.
- The live source has unrelated uncommitted edits in
  `tests/plugins/web/test_web_search_provider_plugins.py` and
  `tests/tools/test_web_tools_config.py`. The migration must not edit those
  files. New behavior coverage belongs in
  `tests/plugins/web/test_parallel_ga_sdk.py`; the dirty files are run but not
  included in migration commits.
- Hermes dependency policy requires `parallel-web>=1.3.3,<2` in declarations.
  The reviewed lock must resolve exactly 1.3.3.
- `web.parallel_search_mode` defaults to an empty string. Explicit config
  wins; otherwise the legacy profile-scoped `PARALLEL_SEARCH_MODE` retains its
  beta meaning (`fast -> basic`, `agentic -> advanced`, `one-shot -> basic`).

## Baseline verification

Command:

```sh
scripts/run_tests.sh tests/plugins/web/test_web_search_provider_plugins.py \
  tests/tools/test_web_tools_config.py \
  tests/tools/test_web_provider_query_logging.py
```

Result on the clean base: 64 passed, 2 failed. The two failures are
`TestParallelClientConfig.test_creates_client_with_key` and
`test_singleton_returns_same_instance`. The active fallback interpreter can
import a newer Parallel SDK, but the base lazy dependency contract requires
exactly `parallel-web==0.4.2`, reports that spec missing, and refuses a lazy
install because `security.allow_lazy_installs=false`. This is the obsolete
dependency state the migration is intended to replace, not a license to edit
the two dirty live tests. Re-run the unchanged files after the declaration and
lock migration; they must pass without network or credential use.

## Task 1 — dependency state

Status: complete and independently approved

- Updated both dependency declarations to `parallel-web>=1.3.3,<2`.
- `uv lock` initially failed because the repository-wide 14-day
  `exclude-newer` window predates the 2026-09-01 release. Added the minimum
  package cutoff `2026-09-01T00:26:00Z`, which admits the reviewed 1.3.3
  artifact but no later upload, then regenerated `uv.lock`. The comment
  requires removing the temporary exception after `2026-09-15`; the absolute
  cutoff does not expire by itself.
- `uv sync --extra parallel-web` installed `parallel-web==1.3.3` into the
  worktree virtual environment. A real, local-only import proved both
  `Parallel` and `AsyncParallel`; no client was constructed and no service
  request was issued.
- The first baseline-suite rerun used the fallback Hermes interpreter because
  the new worktree environment did not yet contain pytest, so it reproduced
  the same two stale-environment failures. After adding the existing `dev`
  extra to the worktree environment (no repository change), the unchanged
  baseline files passed: 66 passed, 0 failed.
- Unchanged packaging metadata tests passed: 14 passed, 0 failed.
- `uv lock --check` and `git diff --check` pass.

Commits: `0585b67281`, follow-up `d35ff0901e`
Report: `.superpowers/sdd/2026-09-02-parallel-ga-migration/task-1-report.md`

Independent review round 1: `NEEDS_FIXES`. The first package-specific cutoff
was broader than necessary and the report inaccurately described an absolute
timestamp as time-bounded. The original implementer tightened the cutoff,
added the explicit removal date, regenerated the lock, and corrected the
report in additive commit `d35ff0901e`.

Independent re-review: `APPROVED`. `uv lock --check`, direct SDK interface
imports, packaging metadata tests, cutoff consistency checks, and
`git diff --check` passed. The lock resolves exactly `parallel-web==1.3.3`;
no credentials were read and no service request was made.

Next dependency-ordered step: Task 2 GA request construction and legacy mode
migration.

## Task 2 — GA provider behavior

Status: complete and independently approved

Commit: `db34af29b7`

- Strict RED-GREEN cycles proved the mode/config contract, direct GA search
  request, and direct GA async extract request before implementation.
- Added empty `web.parallel_search_mode`; explicit valid GA values win, while
  profile-scoped legacy `fast`, `agentic`, and `one-shot` retain beta meaning
  through the documented GA mappings.
- Real locked SDK client specs and response models back the tests; transport is
  mocked and no credential, network, or paid service call is used.
- Focused provider tests: 23 passed. Specified broader migration set: 89
  passed. Additional web config/routing suite: 11 passed; the final combined
  rerun passed all 100 cases. Ruff lint, compileall, and `git diff --check`
  pass.
- Implementation paths are limited to the provider, initializer, config
  default, and new focused test. The live checkout remains untouched.

Report: `.superpowers/sdd/2026-09-02-parallel-ga-migration/task-2-report.md`

Independent review round 1: `NEEDS_FIXES`. Both global Parallel client caches
were returned before resolving the active profile's `PARALLEL_API_KEY`, so a
second or keyless profile could receive the first profile's cached client and
credential; in-process key rotation also remained stale.

Correction:

Commit: `1ae0bbae01`

- Resolve the active profile credential before every cache lookup.
- Reuse a client only when a process-local keyed BLAKE2 fingerprint matches,
  with client/fingerprint pair operations protected by one lock.
- Active/multiplex secret scopes use the authoritative `secret_scope` lookup,
  preventing raw process-environment fallback after a scoped miss.
- Added sync and async tests for A-to-B isolation, A-to-empty fail-closed,
  same-key singleton reuse, rotation, reset-state clearing, non-raw cache
  identities, and profile-aware availability.
- Strict RED: 9 cache-contract failures; intermediate GREEN exposed 2
  scoped-miss failures; availability RED exposed 1 further failure. Final
  focused GREEN: 33 passed.
- Expanded unchanged web/profile suites: 267 passed, 0 failed, 2 skipped.
  An additional API-server profile file has 2 unrelated failures because the
  optional `aiohttp` package is unavailable in this worktree environment.
- Ruff lint/format for the new test, compileall, `uv lock --check`, and diff
  checks pass. No network, service call, real credential access, or
  live-checkout mutation was performed.

Independent re-review round 2: `NEEDS_FIXES`. The credential-cache isolation
defect is closed, but the legacy `PARALLEL_SEARCH_MODE` path still called
`get_provider_env()` directly. Under multiplexing, an authoritative profile
scope containing its own API key but no mode could inherit a conflicting
process mode (`fast -> basic`) instead of the required unset default
(`advanced`).

Second correction:

- Committed additively as `0b55ed3c9f` (`fix: isolate Parallel legacy mode
  by profile`).
- Generalized the proven scope-authoritative Parallel value resolver and use
  it for both `PARALLEL_API_KEY` and legacy `PARALLEL_SEARCH_MODE`.
- Added consumer-level regressions for authoritative scoped mode miss with a
  conflicting process value, scoped explicit legacy mode, and ordinary
  unscoped process-mode compatibility.
- Strict RED: 1 failure (`basic` instead of `advanced`) and 35 passes. Focused
  GREEN: 36 passed. Expanded web/profile rerun: 270 passed, 0 failed, 2
  skipped across 13 files.
- No network, service call, real credential access, live-checkout mutation,
  or change outside the Task 2 provider/test ownership.

Final independent re-review: `APPROVED` with no critical, important, or minor
findings. Authoritative scoped mode misses no longer inherit process/default
state; scoped explicit and ordinary unscoped compatibility remain correct;
the previously approved credential isolation remains closed. Reviewer runs:
141 focused/related tests and 221 expanded web/secret-scope tests passed,
along with lock, Ruff, compile, and diff checks. No network, real credential,
or live-checkout mutation occurred.

## Task 3 — documentation slice

Status: implementation complete; pending independent whole-migration review

- Updated the English and Simplified Chinese web-search feature and
  configuration guides with the same `web.parallel_search_mode` GA contract.
- Documented explicit-config precedence, active-profile legacy mappings,
  `advanced` as the unset result, and `.env` as secret-only for
  `PARALLEL_API_KEY`.
- Recorded the locked `parallel-web==1.3.3` direct `Parallel.search` /
  `AsyncParallel.extract` interface and retirement of the beta surface.
- Obsolete-reference scan, `uv lock --check`, offline SDK interface probe, and
  `git diff --check` pass. No credential, network, service call, live-checkout
  mutation, or code/test/dependency change occurred.
- Website-local Docusaurus scripts were not run because this isolated worktree
  has no website dependencies installed and the task forbids package/network
  access.

Report:
`.superpowers/sdd/2026-09-02-parallel-ga-migration/task-3-docs-report.md`

Remaining dependency-ordered steps: independent whole-migration review,
controlled integration into the active dirty checkout, and live-runtime
metadata/interface verification without a service call.
