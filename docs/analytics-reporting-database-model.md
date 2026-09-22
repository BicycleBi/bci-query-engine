# BCI Analytics database model

Status: source-development contract. Dev and QA only; Production is excluded.

The local successor described in
[the denial-contract handoff](analytics-denial-contract-handoff-2026-09-22.md)
adds `analytics-denial-summary.sql` after the v0.5.0 base schema. Its database
function owns denial identity/audience semantics and bounded aggregation,
independently of account paging. A missing function reports unavailable.
Client presentation metadata (sections, labels, selected metrics and periods)
belongs in the client's canonical render-payload contract; it grants no access
and must not introduce client-specific branches in Query Engine.

## Purpose

BCI Analytics combines Usage reporting with an effective-Access view. The
semantic layer defines what those reports mean. The physical layer records
where the supporting objects live and which service owns them. Keeping the two
layers explicit prevents a client package from copying SRP-specific database or
authorization assumptions into another stack.

## Semantic layer

The semantic contract has two reports under the canonical
`usage-monitoring-dashboard` artifact:

- **Usage** returns bounded activity aggregates for 7, 30, or 90 days. It does
  not return request bodies, report payloads, recipients, or raw telemetry.
- **Access** returns current active identities, effective direct/group grants,
  and artifact relationships. It is a current security snapshot; the selected
  Usage period does not turn it into historical authorization reporting.

The permitted audience labels are `all`, the configured client key, and
`bicycle`. Administrators may select any permitted audience. An approved
client-reporting role is restricted to its configured client audience and may
not use Usage. Missing or mismatched stack configuration fails before a report
query runs.

## Physical layer

| Physical area | Objects and responsibility | Owner |
|---|---|---|
| Metadata database | `security_users`, direct/group role assignments, role permissions, `app.artifacts`, monitoring relations, and the `analytics_reporting` contract/views in `docs/analytics-reporting-schema.sql` | Security owns identity and grants; Query Engine owns the shared semantic views and remains the sole monitoring writer |
| Data database | Client-owned render/report views and role-aware Home views | Client repository owns SQL and its semantic mapping |
| Query Engine transaction context | `bci.authenticated_subject`, `bci.authorized_roles`, `bci.platform_admin_role`, and `bci.platform_corporate_role` | Query Engine binds trusted values for each transaction |
| Runtime configuration | Expected client key, Bicycle administrator role, and optional client-reporting role | Orchestration owns the fail-closed environment binding; it must match the client row installed in `analytics_reporting.client_contracts` |

No client database or client-data rows belong on a developer machine. Direct
inspection remains limited to metadata, schema, grants, counts, hashes, and
non-exposing validation results.

## Relationship between the layers

1. Security authenticates the caller and supplies the client identity.
2. Query Engine verifies that the route client matches the stack's configured
   Analytics client.
3. The database-owned `analytics_reporting` semantic views resolve active,
   unexpired assignments, audience membership, reportable artifacts, wildcard
   permissions, bounded request activity, and authenticated denials.
4. Query Engine binds the authenticated subject and resolved roles into the
   data-database transaction.
5. Client-owned views use that trusted context when their semantic contract
   requires role-aware rendering.
6. Query Engine issues bounded queries only against `analytics_reporting`
   objects and returns the stable Usage and Access API responses.

The physical metadata schema does not itself authorize a report. Authorization
requires the configured client boundary to match the installed database
contract, an active identity, a live assignment, and the exact permission on
the canonical Analytics resource. Query Engine does not reproduce Security's
assignment, audience, or wildcard rules in Python.

## Environment contract

New client stacks use:

- `ANALYTICS_REPORTING_CLIENT_KEY`
- `ANALYTICS_BICYCLE_ADMIN_ROLE`
- `ANALYTICS_CLIENT_REPORTING_ROLE` when a client-reporting audience is approved

Each stack also installs one matching row in
`analytics_reporting.client_contracts`. That row supplies the administrator
role, optional client-reporting role, canonical Analytics artifact and allowed
artifact delivery modes used by the database semantic layer.

SRP retains its existing role variables as a temporary compatibility path.
RF, RAG, and RBP must use the generic variables. A role configured for one
client is never reused for another client's request.

Dev is the first rollout lane for SRP, RF, RAG, and RBP. QA follows only after
the matching Dev contract and client package pass their checks. Production is
outside the current authorization. Existing Home cards, artifacts, RBP items,
and unrelated authorization remain unchanged.

## Validation contract

Synthetic tests must prove:

- missing, partial, or mismatched configuration fails before a report query;
- both Usage and Access require exact live report authorization;
- a client-reporting role can use only Access and is forced to its client
  audience;
- same-named roles and identities from another client are denied;
- SRP's administrator and corporate behavior remains compatible;
- trusted roles are bound to the data transaction without SRP-specific names;
- non-Analytics artifacts retain their existing behavior.
