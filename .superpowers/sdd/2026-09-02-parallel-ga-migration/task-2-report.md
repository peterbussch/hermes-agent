# Task 2 report — Parallel GA provider behavior

## Scope

Owned implementation files:

- `plugins/web/parallel/provider.py`
- `plugins/web/parallel/__init__.py`
- `hermes_cli/config_defaults.py`
- `tests/plugins/web/test_parallel_ga_sdk.py`

The Task 1 dependency files and the two known dirty live-checkout tests were
not edited. The implementation remains in the isolated migration worktree.

Initial commit: `db34af29b7` (`feat: migrate Parallel provider to GA API`)
Review-fix commit: `1ae0bbae01` (`fix: isolate Parallel clients by credential`)
Re-review fix commit: `0b55ed3c9f` (`fix: isolate Parallel legacy mode by profile`)

## Behavior

- Added the empty `web.parallel_search_mode` default so an explicit GA mode
  can win without masking a preexisting profile-scoped legacy value.
- Added pure `_resolve_search_mode(configured_mode, legacy_env_mode)` mapping:
  explicit `turbo`, `fast`, `basic`, and `advanced` remain exact; aliases map
  `agentic -> advanced` and `one-shot -> basic`; legacy beta `fast` maps to
  GA `basic`; absent or invalid input fails closed to `advanced`.
- Search now calls direct synchronous `Parallel.search` and places the capped
  `max_results` value in `advanced_settings`.
- Extract now awaits direct `AsyncParallel.extract` and places `full_content`
  in `advanced_settings`.
- Search/extract response envelopes, positions, excerpt fallback, per-URL
  errors, interruption short-circuiting, and provider error wording remain
  covered by focused compatibility tests.
- Every sync and async client lookup now resolves the active profile's API key
  before examining the shared cache. A cache entry is reused only when its
  process-local keyed BLAKE2 fingerprint matches the current credential.
- Cache client/fingerprint reads and writes are lock-protected, key rotation
  replaces stale clients, and the reset helper clears both clients and both
  fingerprint slots. Raw keys are not stored in the cache identity or logged.
- Under an active secret scope or multiplex runtime, API-key resolution goes
  directly through the authoritative secret scope; this prevents the generic
  provider helper's process-environment fallback after an empty scoped miss.
  Ordinary unscoped single-profile resolution retains config/`.env` behavior.

## Strict TDD receipts

1. Mode/config RED: 14 failed because `_resolve_search_mode` accepted no
   arguments and the new config key was absent. GREEN: 14 passed.
2. GA search RED: 3 failed because the implementation still called
   `client.beta.search`; GREEN: 17 total focused cases passed.
3. GA extract RED: 2 failed because the implementation still awaited
   `client.beta.extract`; GREEN: 19 total focused cases passed.
4. Added characterization coverage for existing interruption and error
   contracts; final focused run: 23 passed, 0 failed.
5. Independent review found the shared sync/async cache was checked before
   current-profile credential resolution. Required profile-switch/reuse/
   rotation/empty/reset tests produced RED with 9 failures and 23 passes.
6. Keyed cache identity fixed seven failures. The remaining two empty-scope
   failures exposed `get_provider_env()` falling back to process environment
   after an authoritative scoped miss. Direct secret-scope resolution for
   active scopes produced GREEN: 32 passed.
7. Provider discovery had the same scoped-miss fallback. Its regression test
   produced RED with 1 failure and 32 passes; routing `is_available()` through
   the same resolver produced final GREEN: 33 passed, 0 failed.
8. Re-review found legacy `PARALLEL_SEARCH_MODE` still used the generic helper
   independently. An active profile with no scoped mode inherited a conflicting
   process `fast` value and resolved `basic`; the regression produced RED with
   1 failure and 35 passes. The API key and legacy mode now share the same
   authoritative scoped-value resolver. Final focused GREEN: 36 passed.

The focused tests import the real locked `parallel-web==1.3.3` clients and
response models. Only client methods are mocked at the request boundary; no
real credentials were accessed and no network or paid service request was made.

## Verification

```text
scripts/run_tests.sh tests/plugins/web/test_parallel_ga_sdk.py
23 passed, 0 failed

scripts/run_tests.sh tests/plugins/web/test_parallel_ga_sdk.py \
  tests/plugins/web/test_web_search_provider_plugins.py \
  tests/tools/test_web_tools_config.py \
  tests/tools/test_web_provider_query_logging.py
89 passed, 0 failed across the three paths present at this base

scripts/run_tests.sh tests/tools/test_web_providers.py
11 passed, 0 failed

Final combined rerun of all four present focused/broader files
100 passed, 0 failed

Expanded web + profile secret-scope verification after review correction
267 passed, 0 failed, 2 skipped across 13 files

Expanded rerun after scoped legacy-mode correction
270 passed, 0 failed, 2 skipped across 13 files

uv run ruff check <four owned implementation paths>
All checks passed

uv run python -m compileall -q <four owned implementation paths>
exit 0

git diff --check
clean
```

As in Task 1, `tests/tools/test_web_provider_query_logging.py` is not present
at this base, so the canonical runner discovered and ran the other three
specified files. A formatter check showed the preexisting provider and large
config-defaults module are not globally formatter-clean; they were not bulk
reformatted because that would create unrelated churn. The new test and
plugin initializer are formatter-clean, and all four owned paths pass Ruff's
lint check.

One extra expanded test file,
`tests/gateway/test_api_server_multiplex_secret_scope.py`, was also attempted.
It produced 1 pass and 2 unrelated failures because this worktree does not
have optional `aiohttp`: one test cannot import it and the other reaches a
module-level `web=None`. No API-server code or environment dependency was
changed; the other 13 web/profile files were rerun without that unavailable
optional surface and passed all 267 cases.

## Self-review

- Request keywords match the inspected 1.3.3 method signatures.
- `PARALLEL_SEARCH_MODE` is read only when explicit config is empty. Active
  profile/multiplex scopes use the authoritative scope directly; ordinary
  unscoped operation uses `get_provider_env`, preserving process and `.env`
  compatibility without permitting cross-profile fallback.
- `PARALLEL_API_KEY` is resolved before cache inspection on every call.
- Both credential identities are keyed process-local digests and are compared
  under the same lock as their client slots; raw credentials appear only in
  the SDK constructor/client state required to authenticate.
- No `.beta.search` or `.beta.extract` remains in the owned plugin files.
- No live checkout integration or service call was attempted.

Independent review round 1: `NEEDS_FIXES` for cross-profile credential/client
reuse; that defect is closed. Re-review round 2: `NEEDS_FIXES` for scoped
legacy-mode fallback. The second correction is implemented and verified;
the complete Task 1–3 migration range received an independent `APPROVED`
verdict with no critical, important, or minor findings. That closure review
ran 336 relevant tests with 3 platform skips, plus Ruff, compileall,
`uv lock --check --offline`, `git diff --check`, and an offline critical-level
npm audit. It also verified the English and Simplified Chinese documentation,
the locked 1.3.3 SDK interface, credential/mode isolation, and the absence of
live credentials, network calls, or live-checkout mutation during review.
