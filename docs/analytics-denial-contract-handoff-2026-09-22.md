# Analytics denial contract — source candidate

Base: v0.5.0 (`b76c0cad06637c8ae3052a16256092052a2dec52`).
Branch: `codex/analytics-denial-contract-20260922`.
Local worktree: `/private/tmp/qe-analytics-denial-contract-20260922`.
Status: committed review candidate; no merge, release tag or host execution.
RF owner note: `BicycleBi/RF`, branch
`codex/rf-analytics-render-contract-20260922`,
`Documentations/rf-analytics-render-contract-handoff-2026-09-22.md`.
Tracking: Teamwork 36431306 / 36431312. Production and SRP sign-out are excluded.

The previous summary queried denials only when its account page was nonempty,
then joined every denial to active registered users. Authenticated subjects
without a matching active account could disappear. Identity fallback also lost
provider-subject/email matches when the subject was nonempty.

`docs/analytics-denial-summary.sql` adds an invoker-rights function over the
existing v0.5.0 semantic objects. It owns identity resolution, audience filtering,
aggregation and a 201-row upper bound. `all` preserves unmatched/inactive
subjects; narrower audiences require current same-client membership. Exact known
subjects win; email fallback requires an unknown provider subject and a unique
same-client match. Known foreign subjects and ambiguous email remain unmapped.
The result exposes only aggregate account/artifact labels, count and timestamp.

`app/usage_access.py` calls that function independently of account search/paging
and request activity, returning 200 rows plus explicit availability/truncation.
Existing report authorization, matrix semantics and client isolation remain.
RF's additive SQL `116_analytics_denial_summary.sql` must remain byte-identical.
Missing SQL returns denial-unavailable, never the previous incomplete aggregate.

Validation: full Python suite 171 passed; outer SQL and function body parse as
PostgreSQL. New tests cover empty/search-filtered/later account pages, independent
availability, bounded output and audience binding. Local tests mock database
execution; they do not establish PostgreSQL semantic or client runtime acceptance.
`tests/analytics_denial_summary_synthetic.sql` is an unexecuted, guarded,
transactional secured-host rehearsal returning fixed pass/fail only. It requires
a new disposable `analytics_synthetic_*` database with no existing source objects;
never execute it against a client database.

Before release: review/rehearse SQL; preserve no-grant-change scope; publish only
after explicit approval; issue a new reviewed engine tag; install the exact RF
schema/template/data-shell/runtime migration set through current BCI Promote.
Do not move v0.5.0 or edit historical package hashes. RF's handoff records the
full remaining validation, package and acceptance work. No environment sequence
or execution authority is implied by these local checks.
