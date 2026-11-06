# Persistent artifact payload cache — SRP Dev

User authorized finishing Redis caching/prewarming for Referral while preserving report and QC output. Owner: SRP rebuild chat. Source base `ab9c9198` matches installed Dev hashes for engine, routes, cache adapter and Redis wrapper; preserve all existing facility/access, PPP, delivery and sibling behavior. Local worktree `/private/tmp/bci-query-engine-referral-payload-cache-20261106`, branch `codex/referral-payload-cache-20261106`. The unrelated main checkout has user-owned merge conflicts and is untouched.

## Contract

`app/payload_cache.py` adds explicit opt-in via `<existing-view>_payload_cache_policy()`, currently only `{"mode":"load_bound","scope":"shared"}`. A view opts in only when its canonical final rows are identical for every authorized report viewer. Existing client/artifact authorization and trusted SQL context remain required on each public request. Policy absence keeps the old serving path. A future identity-dependent payload must not use this shared policy.

The persistent entry contains only canonical final presentation rows plus generation metadata; it owns no metrics, filters, QC or source preparation. Its key includes client, artifact, view and presentation-definition fingerprint. One Redis SET through a Lua compare-and-set replaces the whole value with no TTL. A late older prewarm cannot overwrite a newer generation. A failed write leaves the prior value intact; successive loads replace one key instead of accumulating full copies. Existing per-identity rendered HTML cache remains separate and uses the active payload generation. Template changes still bind rendered-cache keys.

An internal service-authenticated POST `/internal/artifacts/{client}/{artifact}/payload-cache/prewarm` reads a repeatable canonical snapshot and accepts optional `expected_freshness`. It returns metadata only. It never renders, delivers, invokes QC or acquires/rebuilds source/semantic data. Superseded requests are explicit. Redis loss can use the existing SQL path over prepared presentation data; a cold cache can rehydrate from that data without raw-source work. Manual refresh keeps its explicit refresh behavior.

## Release and evidence

Runtime recipe: `deployment/promotions/dev-srp-referral-payload-cache-runtime.yml`. Exact source only; existing BCI Promote SRP Dev runtime primitive builds and recreates Query Engine alone, without registry images, new services or global cache configuration. Prepare/check alongside the SRP cache-policy/chain and isolated DI packages before first apply. Runtime first (inactive without opt-in), then policy/chain metadata, then independent DI consumers. Preserve old runtime source `ab9c9198` and engine-owned image/recovery evidence for governed rollback. No QA/Production scope.

Source regression: cache persistence/atomic replacement/older-generation rejection, Redis failure, exact rows, absent/shared policy, cache-key isolation, zero SQL reads after rendered-cache expiry, service-token enforcement and non-exposing errors. Existing cache/auth regressions remain required. Test process preloads actual requests/psycopg before running older cache tests because those tests install module stubs when absent; this is test-harness isolation, not a runtime change.

Operational ownership and exact release set live in SRP `docs/handoffs/srp-referral-persistent-cache-2026-11-06.md`. No report/template/QC logic is changed. No live payload or client values are recorded here. Retain this clean worktree through Dev validation and source handoff; cleanup review after acceptance and remote preservation.
