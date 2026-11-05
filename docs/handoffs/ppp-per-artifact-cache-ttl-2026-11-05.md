# PPP per-artifact query-cache TTL — 2026-11-05

Status: superseded for the SRP QA release. The separate implementation in `b912a79` was reverted after the user challenged its necessity. The QA release must use Jeanre's `b9d6e35` load-bound generation implementation instead. This file preserves the prior proposal and does not authorize promotion.

## Chat agreement record

| ID | Date and source chat | User words | Agent interpretation | Agreement status |
| --- | --- | --- | --- | --- |
| QE-PPP-TTL-1 | 2026-11-05, current Codex PPP QA cache chat | “so can we change the query engine so that it keeps the default but we can overwrite that default in a setting per report?” | Permit an artifact-owned query-cache TTL override while retaining the runtime default for artifacts without a setting. | Question proposing a design, not implementation authority. |
| QE-PPP-TTL-2 | 2026-11-05, current Codex PPP QA cache chat | “lets do that” | Implement QE-PPP-TTL-1, allow PPP's existing policy to request no expiry, and replace stale PPP entries after a completed load. | Explicit implementation request. |
| QE-PPP-TTL-3 | 2026-11-05, current Codex PPP migration chat | “okay migrate this plus jeanres changes to qa” | Include the QA-reconciled Query Engine cache-generation dependency in the PPP QA release, without changing other reports' TTL defaults. | QA migration request. |

The previously requested PPP full-set filter cache remains the consumer. This change does not alter SQL metrics, other reports' TTL, the global default, schedules, deployment policy, or runtime configuration.

## Object-level scope ledger

| Object | Requested outcome | Why required |
| --- | --- | --- |
| This handoff | QE-PPP-TTL-1, QE-PPP-TTL-2 | Preserve user words, scope, implementation evidence, and handoff state. |
| `app/engine.py` artifact query-cache policy resolution and prewarm | QE-PPP-TTL-2 | Read the per-artifact policy, select the TTL for query and prewarm writes, preserve default fallback, and report the actual refresh mode after stale entries are replaced. |
| `bci_redis_cache/cache.py` cache write and scoped replacement | QE-PPP-TTL-2 | Interpret a selected TTL of zero as no expiration and atomically replace only this artifact's query entries after the replacement values are ready. |
| Focused cache and engine tests | QE-PPP-TTL-2 | Verify default fallback, PPP-like override, no-expiry write, and prewarm replacement using synthetic fixtures. |
| `codex/per-artifact-query-cache-ttl-20261105` worktree/branch | QE-PPP-TTL-2 | Isolate this change from unrelated Query Engine work. |
| `deployment/promotions/qa-srp-ppp-cache-generation-20261105.yml` | QE-PPP-TTL-3 | Governed QA Query Engine runtime member required for PPP's persistent atomic cache generation. |
| QA protected package and source/handoff refs | QE-PPP-TTL-3 | Bind exact source, baseline, hashes, and runtime validation for QA. |
| `deployment/packages/srpqa-ppp-cache-generation-20261105/promotion.json` | QE-PPP-TTL-3 | Immutable protected runtime package for the exact QA cache-generation change. |

Excluded: global TTL/configuration changes, other cache types, other artifacts' policy sources, SQL metrics, periodic jobs, promotion policy, and environment mutations.

## Workspace

- Repository: `BicycleBi/bci-query-engine`
- Worktree: `/Users/Daniel/.codex/worktrees/ppp-query-cache-ttl`
- Branch: `codex/per-artifact-query-cache-ttl-20261105`
- Base: `origin/main` at `9adc72095420066c43485a6654790890c9b376de`
- Cleanup condition: retain until this correction is committed, reviewed through its own lifecycle, and present in the intended runtime.

The implementation was committed and pushed on the main-based source branch at
`a70e3bd253aa8e668a6281c035e9d14c80a67781`; it was **not merged to main**.
For QA, the same change was reconciled separately onto `origin/qa`
`16b38d975bb0682299a6940629dbbb2b9bb28bc1` and pushed as
`codex/ppp-query-cache-ttl-qa-20261105` at
`b912a7946dd0c4feb649329b9d3db4600d866cff`. No PR, merge, or runtime
promotion has occurred.

## Local validation

- Focused cache/engine tests: 31 passed.
- Full Query Engine suite: 215 passed when the installed HTTP library is imported before test stubs. The ordinary invocation has a pre-existing test-import-order issue causing four unrelated failures; those tests pass separately.
- QA-reconciled Query Engine suite: 194 passed with the same HTTP-library import ordering.
- Default behavior: positive runtime TTL remains unchanged when no artifact policy exists.
- PPP-style policy: `ttl_seconds: 0` writes no Redis expiry; `prewarm_swap` publishes prepared entries with a scoped Redis transaction and returns that receipt.
- No live service or database was changed or queried.
