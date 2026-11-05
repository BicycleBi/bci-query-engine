# PPP load-bound cache generation — QA source

## Agreement and correction

- ID: PPP-QE-QA-20261105-01; date: 2026-11-05; source: current SRP PPP migration chat.
- User words: “okay migrate this plus jeanres changes to qa”
- Agent interpretation: move the working PPP report and Jeanre's completed-load cache behavior to QA.
- Status: requested QA migration; no promotion authority or validation is implied by this note.
- ID: PPP-QE-QA-20261105-02; date: 2026-11-05; source: same chat.
- User words: “query engine changes was said is not needed?” and “so keep going baby”
- Agent interpretation: exclude the separately authored per-report TTL implementation. Jeanre's own Query Engine generation code is required by his worker and PPP cache policy.
- Status: correction requested and being prepared; no QA apply has occurred.

## Object-level scope ledger

| Object | Requested outcome | Why required |
| --- | --- | --- |
| `app/engine.py` | Jeanre's load-bound PPP cache behavior | Reads the artifact policy, serves the active Redis generation, and publishes a new one only after a complete prewarm. |
| `app/main.py`, `app/models.py` | Jeanre's load-bound PPP cache behavior | Preserve the prewarm endpoint contract consumed by his worker. |
| `bci_redis_cache/cache.py` | Jeanre's load-bound PPP cache behavior | Stores persistent generation entries and atomically switches the active pointer. |
| Focused Query Engine tests and README contract | Verify Jeanre's behavior | Preserve and check the exact runtime contract without unrelated changes. |
| `deployment/promotions/qa-srp-ppp-cache-generation-20261105.yml` | Governed QA runtime delivery | Scoped QA service promotion using the existing package path. |
| QA source/package refs and immutable package | Governed QA runtime delivery | Pin baseline, content hashes, source and package revisions. |
| This handoff | Durable scope and correction evidence | Prevent the superseded implementation from returning in promotion. |

Jeanre's source commit is `b9d6e35` ("Add load-bound query cache generations"). Its runtime-file patch ID matches this QA branch's runtime-file delta against `origin/qa`. The separate implementation `b912a79` is excluded from the final tree. No QA environment was mutated while preparing this correction.
