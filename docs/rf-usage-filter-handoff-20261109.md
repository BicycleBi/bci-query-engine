# RF Dev Usage filter dependency

Owner Andrew; Teamwork36716106 under36708302. Scope: bounded Usage reads only,
RF Dev deployment only. Andrew explicitly approved the API dependency and RF Dev
Query Engine release. No other client upgrade, QA/Production, authentication,
role/grant, telemetry writer, Home or framework/policy change.

Branch codex/rf-usage-filters-20261109; base origin/main9adc72095420066c43485a6654790890c9b376de.
Local worktree: sibling query-engine-rf-usage in the RF review workspace. Retain
until source release/review/remote and process custody is resolved.

## Interface

Existing 7/30/90 rolling defaults preserved. Optional start_date/end_date are
inclusive UTC calendar days, both required, maximum90 days, no future end date.
Optional audience=all/client-key/bicycle, artifact_search/user_search<=100 chars.
Parameterized predicates apply before aggregate and top20/top30 bounds.
Audience uses current database-owned authorized account membership; all retains
unclassified telemetry. Non-admin reporters cannot select Bicycle-only results;
their existing default all-client aggregate behaviour is preserved.
Artifact search matches key/display name for requests and interactions. Login
outcomes have no artifact grain and use dates/audience/user only. No raw telemetry
is returned. applied_filters acknowledges normalized filters; period_start/end
acknowledge custom dates. Date end is exclusive in the API response.

## Evidence and remaining release gate

131 local focused tests pass across Usage, identity mapping, access/authentication,
reporting wildcard authorization and configuration, including PostgreSQL syntax
parsing and placeholder/parameter arity. One existing Starlette deprecation warning.
These use synthetic/mocked sources; no client database was opened locally.
RF source includes the wired UI and local browser evidence in:
RF Documentations/working-notes/2026-11-09-analytics-call-revisions.md.

Deployment policy requires an exact semantic tag on reviewed remote main history
(docs/deployment/repository-built-container-images.md in orchestration). This
feature branch is not a releasable runtime. Prepare/review this PR and publish a
new semantic source tag through the owning repository's authorized route; do not
build from the branch or retag v0.5.3. Then qualify the existing orchestration-owned
RF Dev recipe, exact retained predecessor recovery and paired RF content package.
No environment changes or live validation are claimed by this checkpoint.

RF content must follow API readiness and pass live custom-date, audience, search,
chart/dialog, unavailable/empty, desktop/mobile and Security Access regression
checks. Keep all other clients paused until revised reports are reviewed.
