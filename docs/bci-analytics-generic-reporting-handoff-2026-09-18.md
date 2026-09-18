# BCI Analytics generic reporting handoff

## Current state

- Last reviewed: `2026-09-18`
- Active branch: `codex/bci-analytics-generic-reporting-36413928`
- Base: `origin/main` at `4f95598`
- Scope: shared Query Engine source and synthetic tests only
- Environments changed: none

The generic reporting contract is implemented and the full local test suite
passes. Dev and QA source/setup work is authorized; Production is excluded.
No runtime package, environment configuration, grant, database object, Home
artifact, service, or deployment inventory has been changed.

## What changed

- Added fail-closed per-stack configuration for the expected Analytics client,
  Bicycle administrator role and optional client-reporting role.
- Applied the same live report authorization evaluator to Usage and Access for
  every configured client.
- Replaced SRP-only audience values with `all`, the actual client key and
  `bicycle` while preserving the existing SRP compatibility path.
- Bound generic Analytics roles into the trusted data-database transaction
  context without reusing one client's configuration for another client.
- Documented the semantic Usage/Access model and the physical metadata/data
  database ownership model.
- Added synthetic coverage for missing, partial and cross-client configuration,
  generic live authorization, artifact-route gating and database role context.

## Useful files

- `app/analytics.py`
- `app/usage_access.py`
- `app/main.py`
- `app/engine.py`
- `docs/analytics-reporting-database-model.md`
- `tests/test_analytics_config.py`
- `tests/test_usage_access.py`
- `tests/test_engine_cache.py`

The cross-repository rollout plan is:

- repository: `bci-container-orch`
- branch: `codex/bci-analytics-rollout-36413928`
- path: `docs/notes/bci-analytics-rollout-36413928-plan-2026-09-17.md`

## Verification

Run from this repository with development dependencies available:

```bash
python3 -m pytest -q
```

Latest result: `159 passed`. Two dependency deprecation warnings are present;
there are no test failures.

## What still needs attention

- Jeanre or the designated Query Engine maintainer must review the source PR.
- Confirm the new RF Access, RAG Usage/Access and RBP Usage/Access audiences
  before adding grants or client-reporting role configuration.
- Prepare RF Dev first, then RAG Dev and RBP Dev. Preserve every existing RBP
  item; RBP Operations interactions remain excluded unless explicitly approved.
- QA follows accepted Dev evidence. Production remains outside authorization.

## Suggested restart

1. Review `docs/analytics-reporting-database-model.md` and `app/analytics.py`.
2. Run the complete test suite.
3. Review the branch against `origin/main`.
4. After approval, pin the accepted commit in the bounded RF Dev package.
5. Use BCI Promote for any later environment plan/check/apply/validate cycle.
