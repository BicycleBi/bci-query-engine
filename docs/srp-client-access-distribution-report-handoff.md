# SRP client access and distribution reporting

Date: 27 October 2026
Owner/requester: Daniel Van Wyk
Client task: https://tw.bicyclebi.com/app/tasks/36603566
Branch: codex/srp-access-distributions
Base: origin/main c0f932b
Client source: BicycleBi/SRP, codex/srp-client-access-distribution-report-20261026

## Authorized scope and object ledger

Daniel requested duplicating SRP User Access, adding current distributions, showing actual email addresses, preserving the existing viewer model and applying in Dev. No QA/Production, recipient/access/schedule changes or policy expansion.

- `app/distribution_reporting.py`: required read-only bounded Metadata/Data source projection and approval comparison. No sends or mutations.
- `app/main.py`: import and new SRP combined-artifact distribution-summary route only; retain existing analytics authorization and client validation. Existing access route remains untouched.
- `tests/test_distribution_reporting.py`: synthetic checks for permission denial, audience separation, source unavailability, unresolved approval and comparison behavior. No client contacts/payloads inspected.
- This handoff: durable service implementation and deployment evidence.
- Existing `deployment/promotions/dev-srp-query-engine-runtime.yml`: reused unchanged, not a new policy or runtime configuration path.
- Dedicated clean host alias/source refs: pin this commit for the governed source build; routine BCI Promote execution stays with bcipromote.

## Behavior

GET `/artifacts/srp/client-access-distribution-report/distribution-summary` calls the existing `require_analytics_reporting_access(..., 'access')` before data reads. It is client/artifact-specific, current-state only, no-store, search/facility/result filtered and paged at 50 rows, with at most 5000 rows per source. Missing registry/audience data fails; missing report-specific optional sources returns explicit partial coverage. Known Bicycle admin recipient identities are excluded using the existing reporting audience model. Shared recipients are not guessed into portal users.

Configured intent reads Metadata artifact recipients plus Weekly mapping (also inherited by Monthly), P&L scope/effective recipient source/overrides. It distinguishes mapping-only configuration and missing registry targets. No raw contacts are added to the repository. Monthly source-activity eligibility, consolidated scopes and workflow group selections remain unverified and are never reported as complete. Configuration is not delivery proof.

Approval relation `public.srp_report_distribution_approval` is optional and is not created or populated in this change. Its expected fields are in the query. Missing approval remains Not confirmed. Complete supplied provenance permits explicit matches/missing/explicit exclusions/scope differences; absent recipients are not classified extra from an incomplete list. Do not infer an exhaustive approval set. Approval import/maintenance requires SRP's confirmed evidence; no approval is supplied here.

## Validation and release status

71 synthetic tests passed across distribution reporting, existing access and usage summary suites. No environment has been changed at this checkpoint. Native runtime plan/check, exact host-source/live-runtime compatibility, apply, validation, safe authenticated status checks and canonical Dev closeout remain required. Source test success is not deployment or approved recipient evidence.
