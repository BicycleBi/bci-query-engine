# BCI Analytics database-driven reporting redesign

## State

- Repository: `BicycleBi/bci-query-engine`
- Branch: `codex/database-driven-analytics-access`
- Base: Query Engine `v0.4.0`, commit `10c11bdacde947ec04122228f4e3aa572cba1c12`
- Scope: shared source, database contract, documentation and synthetic tests
- Environment changes: none

## Decision

BCI Analytics access semantics belong in the metadata database, not in
client-specific Query Engine Python. Query Engine remains the authenticated,
bounded API boundary and response shaper. The database owns the reusable
semantic and physical access model.

## Implementation

`docs/analytics-reporting-schema.sql` adds:

- one per-client `analytics_reporting.client_contracts` row;
- current effective direct/group assignments with expiry handling;
- Bicycle/client audience membership;
- client-relevant effective grants;
- reportable artifact scope from database configuration;
- exact and wildcard artifact-access relationships;
- bounded request-activity inputs; and
- authenticated-denial metadata inputs.

`app/usage_access.py` now queries only those database-owned semantic objects.
It no longer joins Security tables, classifies audiences, resolves wildcard
resources or contains an SRP-specific artifact predicate. The existing runtime
environment binding remains as an independent fail-closed stack boundary and
must match the database contract.

The Access summary accepts a permitted audience without requiring a matrix
perspective. This supports the compact RF administrator report while the
existing SRP user/artifact matrix contract remains unchanged.

## Validation

- Complete local suite: `161 passed`.
- The database schema parses as PostgreSQL through `pglast`.
- Tests prove Query Engine reads only `analytics_reporting` semantic objects.
- Tests retain exact report authorization, client/audience isolation, search
  and paging bounds, wildcard grant detail limits, monitoring-unavailable
  behavior and non-exposing errors.

## Required rollout work

1. Review and merge this source change; publish a new Query Engine patch tag.
2. Add a governed metadata-schema package and RF QA client-contract row.
3. Update RF PR #98 and orchestration PR #2131 to the accepted commit/tag and
   schema prerequisite before any RF QA activation.
4. Create a separate SRP task to install the same schema, seed SRP's contract
   (`web` artifacts only), retest both access perspectives and all three
   audiences, and then update SRP's governed runtime package.
5. Keep Production outside this work until Dev and QA evidence is accepted.
