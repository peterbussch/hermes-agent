# Task 3 documentation report — Parallel GA migration

## Scope

Owned documentation files:

- `website/docs/user-guide/features/web-search.md`
- `website/docs/user-guide/configuration.md`
- `website/i18n/zh-Hans/docusaurus-plugin-content-docs/current/user-guide/features/web-search.md`
- `website/i18n/zh-Hans/docusaurus-plugin-content-docs/current/user-guide/configuration.md`

No provider code, tests, dependencies, lock state, live checkout, profile,
credential, or runtime state was changed.

## Documentation contract

The English and Simplified Chinese docs now agree that:

- new configuration uses `web.parallel_search_mode` in `config.yaml`;
- accepted GA modes are `turbo`, `fast`, `basic`, and `advanced`;
- a nonempty explicit config value takes precedence;
- when explicit config is empty, the active profile's legacy
  `PARALLEL_SEARCH_MODE` is migrated as `fast -> basic`,
  `one-shot -> basic`, and `agentic -> advanced`;
- when neither input is set, Hermes uses `advanced`;
- `PARALLEL_SEARCH_MODE` is compatibility-only and should not be added to new
  `.env` files; only the secret `PARALLEL_API_KEY` belongs there; and
- the reviewed adapter uses the direct `parallel-web` 1.3.3 GA
  `Parallel.search` and `AsyncParallel.extract` methods, not the retired beta
  surface.

## Verification

```text
obsolete-reference scan across the Parallel provider, dependency files,
lockfile, English docs, and Simplified Chinese docs
PASS: no parallel-web==0.4.2 or .beta.search/.beta.extract match

uv lock --check
PASS: resolved 249 packages

uv run --offline Python interface probe
PASS: parallel-web==1.3.3; Parallel.search and AsyncParallel.extract callable

git diff --check
PASS
```

The repository exposes Docusaurus build/typecheck and diagram-lint scripts,
but this isolated worktree has no website `node_modules`; no network or package
installation was permitted for this task, so those scripts were not run. The
Task 3 controller should include the docs in its later full migration review
or build verification when the checked-in website toolchain is available.

## Safety and remaining work

- No credential was read or printed and no Parallel client/service request was
  made.
- The live dirty checkout was not touched.
- This report covers only the documentation slice. Independent whole-migration
  review, controlled live-checkout integration, and runtime verification remain
  with the Task 3 controller.
