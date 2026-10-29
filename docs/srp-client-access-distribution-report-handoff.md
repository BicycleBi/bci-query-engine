# SRP client access and distribution reporting

Date: 27 October 2026
Owner/requester: Daniel Van Wyk
Client task: https://tw.bicyclebi.com/app/tasks/36603566
Branch: codex/srp-access-distributions-dev-20261027
Base: exact running Dev application source 10fa9ee57c37c5af67a27e3a3c625099f3a7b232
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

## Exact Dev baseline reconciliation

Before any apply, secured-host source hashes identified the live application as 10fa9ee5. Latest main c0f932b has unrelated load-action runtime content not present live. The exact endpoint/module/tests were transplanted onto the live baseline on this Dev branch; no load-action change is included. Initial native plan/check on aef2a3d was non-mutating and is superseded. Repeat both packages' native plan/check against the final source before apply. Metadata/schema-only preflight confirmed recipient fields and zero existing-viewer coverage gaps without contact or payload inspection.

## Actual Dev deployment — 27 October 2026

Scope ledger: update this existing handoff only to record validation and deployment; no additional runtime changes. Applied source 2298601fb29f41525d9eb3693981d0c7c08bf5e3 on exact live baseline 10fa9ee5. Native plan/check/apply/validate passed; runtime release dev-20261027T091949Z-a6c60b72, generation 1426. Combined client release dev-20261027T092550Z-eb43596d validated generation 1427. Safe read-only validation returned 52 configured rows and unauthenticated HTTP 401; browser pagination passed. Approval remains Not confirmed and source coverage partial as above. Evidence lives in SRP deployment/reviews/client-access-distribution-report-20261027/. No main/QA/Production integration or runtime configuration expansion. Retain this Dev source branch/worktree for feedback.

## Facilities implementation ledger — 28 October 2026

Daniel requested a facility inventory retaining RCM styling. Objects: new app/facility_reporting.py read-only bounded source union, existing app/main.py import plus existing-access-authorized facility-summary GET, new tests/test_facility_reporting.py synthetic behavior tests, and this handoff. SRP template/package/evidence tracked by its existing handoff. No source-data writes, new tables, recipients, mappings, permissions or schedules. Optional public.srp_report_facility_approval is read only if present; never created/populated. Operational availability means appointment/claim presence in current loaded inputs, not active-practice approval or a low-volume threshold. Monthly eligibility uses canonical report coverage, not reimplemented metrics.

Qualification refinement: operational data presence projects only facility IDs from current retained core appointments/claim-activity inputs; it does not execute report measure calculations or inspect PHI. Canonical Weekly/Monthly report presence and Monthly eligibility still use existing report views and may explicitly return unavailable under the bounded 10-second source timeout. Do not change report calculations, caches or source indexes as part of this inventory feature. No adjacent report/data pipeline changes are authorized.

Facilities Dev deployment completed 28 October: applied runtime source 69d55ad17392347a9883279136e67f3096ef7ccf. Governed runtime plan/check/apply/validate passed; release dev-20261028T041932Z-b95204dd, generation 1441. Client source 5a5df396c05b6c67a80208ccf9adb55452108d50 applied as dev-20261028T042044Z-ad78d44c, generation 1442. 81 synthetic tests passed; live module SHA matched and unauthenticated facility endpoint returned 401. Safe validation returned 29 facilities with explicit partial source coverage. Canonical Weekly rendered-slice and Monthly coverage queries exceeded their ten-second bound; they remain unavailable, not inferred. Existing current core operational presence, saved mappings, P&L sources/crosswalk/scope and recipient registration were available. Approval source absent. Existing report/effective-access fingerprints unchanged. SRP evidence: deployment/reviews/client-access-distribution-facilities-20261028/. No adjacent source/report/calculation/index, permission, recipient, schedule, Home or QA/Production changes. This documentation receipt does not change the applied runtime source.

## Facilities simplification/source-check ledger — 28 October 2026

Daniel authorized concise Facilities business statuses and source-check repair. Change only app/facility_reporting.py, its synthetic test file and this handoff. Replace heavy rendered report projections with exact ready saved mapping/current roster and current operational row presence in the existing Monthly report month. Project presence, not measures; do not alter report SQL/indexes/caches/loaders or email delivery. Add business statuses independent of generated email registration; preserve existing authorization, SRP-only/current-state scope, unions and unknown approvals. Existing runtime spec stages this exact service source through governed Dev workflow. SRP template/spec/evidence objects are ledgered in the client handoff. No user/policy/recipient/grant/schedule/QA/Production changes.

Facilities source-check repair deployed in Dev: exact runtime source 91f7f6630b1b10ca5a8d1d9672010b03d4b91df8, release dev-20261028T063904Z-b6c918da, native validation generation 1448. Client 07e55b05 applied/validated as dev-20261028T064332Z-4f093fec, generation 1449. Both packages passed native plan/check before either apply. 91 tests and actual-query synthetic month boundaries passed. Actual installed module hash matches, unauthorized endpoint 401, all eleven required Facility sources available, 29 facilities returned in 3.53 seconds. Optional approval remains absent. Existing report/effective-access fingerprints unchanged. Authenticated simplified Facilities/filter, original Access and Distributions checks pass. Evidence retained in SRP deployment/reviews/client-access-facility-simplification-20261028/. No adjacent runtime objects changed; this receipt is documentation only.

## Excel reference import ledger — 29 October
Daniel requested importing workbook contents into a report reference table. Runtime baseline is current live 59b643a main hash eea96ab0c013dc4639ce8aa1ceed01adaa832c4add0f27ce298f9fdda5519eec; preserve QuickBooks actions and existing routes. Objects: facility_reporting.py with bounded read helpers inline, Facilities GET import/route in main.py using unchanged authorization, synthetic facility tests and this handoff. Add only fixed reference-table projection and exact-name attachment; reference-only/ambiguous rows remain visible. References never drive live states or approval. Client ledger owns migration/table/template. No identities, access definitions, recipients or live report calculations change.
