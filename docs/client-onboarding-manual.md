# Client-Agnostic Query Engine Onboarding

Status: Active service onboarding guide
Scope: reusable Query Engine instructions for any client and environment

Use this guide to prepare a client's data contract, artifact definitions, and
validation evidence. Client identity, business logic, branding, authorization,
recipients, and schedules belong to the owning client repository and reviewed
environment configuration. Query Engine's service code stays client-agnostic.

The examples use the fictional client `example`, synthetic summary rows, and
a non-deliverable `example.invalid` address. They are API and rendering
examples, not a client stack to clone or an instruction to deploy a demo client.
Replace example identities and contracts with the approved onboarding inputs.

## 1. Read The Controlling Workflow

This guide owns the Query Engine onboarding contract. It supplements the
orchestration workflow; it does not define another deployment path.

| Work | Controlling document in `BicycleBi/bci-container-orch` |
| --- | --- |
| Client artifact development | [Client Development Runbook](https://github.com/BicycleBi/bci-container-orch/blob/main/docs/development/client-development-runbook.md) and [Client-Agnostic Artifact Development Framework](https://github.com/BicycleBi/bci-container-orch/blob/main/docs/development/client-agnostic-artifact-development-framework.md) |
| New environment boundary | [Initial Client Stack Setup Runbook](https://github.com/BicycleBi/bci-container-orch/blob/main/docs/deployment/initial-client-stack-setup-runbook.md) |
| Environment deployment and validation | [Deployment Checklist](https://github.com/BicycleBi/bci-container-orch/blob/main/docs/deployment/client-environment-deployment-checklist.md) and [AI Deployment Runbook](https://github.com/BicycleBi/bci-container-orch/blob/main/docs/deployment/ai-client-environment-deployment-runbook.md) |
| QA or Production release content | [QA and Production Promotion Runbook](https://github.com/BicycleBi/bci-container-orch/blob/main/docs/deployment/qa-production-promotion-runbook.md) |
| Data and operating boundaries | [Development and Deployment Guardrails](https://github.com/BicycleBi/bci-container-orch/blob/main/docs/security/development-deployment-guardrails.md) and [Database Access Safety Policy](https://github.com/BicycleBi/bci-container-orch/blob/main/docs/security/database-access-safety-policy.md) |

Read those files in the reviewed orchestration source used for the work. The
links identify the repositories and paths; a local checkout may be on another
revision. Use the installed workflow's current command, policy, and authority
requirements when executing work.

Client databases and client data loads stay on the secured host development
stack. Local work consists of source editing, static checks, and synthetic
fixture rendering. AI-visible validation follows the controlling data policy:
metadata/schema checks and reviewed non-exposing outputs, rather than raw
client rows, rendered client reports, recipient details, or secrets.

## 2. Record The Onboarding Inputs

Resolve these inputs before preparing changes. Record non-secret references;
keep secret values in the approved secret-management path.

| Input | Synthetic example | Actual source |
| --- | --- | --- |
| Client key and display name | `example`, Example Company | Approved client identity |
| Environment | `dev` | Requested lane: `dev`, `qa`, or `production` |
| Owning client repository and branch | `<client-repo>`, `<source-branch>` | Repository rules and release lineage |
| Host, hostname, technical key, runtime path, port allocation | `<reviewed-environment-allocation>` | Orchestration setup spec and deployment inventory |
| First artifact and template key | `weekly-summary` | Client artifact contract |
| Schema-qualified render relation | `rpt.weekly_summary` | Client-owned reporting migration |
| Columns and output grain | One team/week; `report_week`, `team_name`, `completed_items` | Reviewed data contract |
| Authentication and authorization | `<approved-audience-and-resource-scope>` | Security configuration and authorization inventory |
| Delivery audience and sending configuration | `<approved-test-audience>` | Reviewed delivery metadata and Email Service configuration |
| Refresh and delivery ownership | `<owner-and-schedule>` | Client DI and scheduler contracts |
| Acceptance and validation | `<upstream-acceptance-and-safe-checks>` | Governing lane workflow and client handoff |

Do not copy another client's ports, credentials, roles, routes, report views,
branding, or recipient lists. Query Engine does not require client-specific
service code to onboard a new client.

## 3. Establish Or Verify The Environment

For a new environment, use the orchestration initial-stack workflow and its
[generic setup specification example](https://github.com/BicycleBi/bci-container-orch/blob/main/docs/deployment/initial-client-stack-setup-spec.example.yml).
The documented preflight shape is:

```bash
/path/to/clean/bci-container-orch-worktree/scripts/bci-promote \
  stack-setup-preflight --spec <reviewed-initial-stack-setup.yml>
```

Preflight is read-only. Initial apply is a separate administrator operation
under that runbook's authorization requirements. For an existing environment,
verify its inventory and use the current governed content/runtime workflow;
do not bootstrap it again.

The required repositories and services come from the orchestration
[repository catalog](https://github.com/BicycleBi/bci-container-orch/blob/main/docs/architecture/repositories.md)
and the selected environment's reviewed configuration.

| Component | Onboarding responsibility |
| --- | --- |
| Postgres | Metadata storage and the approved client reporting database |
| Query Engine | Generic artifact storage, rendering, execution, and run/output logging |
| Security and ingress | Authenticated client boundary, audience/resource permissions, and public routes |
| Data Integration | Client source acquisition, transformation, report-ready contracts, and refresh |
| Email Service and credential helper | Governed sending configuration and delivery evidence |
| Redis, scheduler, and monitoring | The caching, schedule, and observation contracts selected by the environment |

Query Engine's database connections are configured through `METADATA_DB_*`
and `DATA_DB_*`; resolved values belong to the environment. Email Service and
security configuration likewise come from that boundary. Use approved secret
references; do not print filled environment files or expanded Compose output.

## 4. Prepare The Data And Rendering Contract

The client repository owns the schema-qualified relation named by `view_name`.
Choose its schema from the approved client model; `public` is not a universal
requirement. Query Engine reads that relation using its configured Data
connection and provides the resulting dictionaries as `rows` to Jinja2.
`columns` contains the first row's column names.

For the synthetic example, the relation `rpt.weekly_summary` has this shape:

| Column | Type | Example |
| --- | --- | --- |
| `report_week` | date | `2026-01-05` |
| `team_name` | text | Example North |
| `completed_items` | integer | `25` |

This query illustrates the fixture values that a reviewed view migration
could use. It is not a live-data validation query:

```sql
SELECT DATE '2026-01-05' AS report_week,
       'Example North'::text AS team_name,
       25::integer AS completed_items
UNION ALL
SELECT DATE '2026-01-05', 'Example South'::text, 18::integer;
```

For local template checks, use
[weekly-summary-rows.json](examples/onboarding/weekly-summary-rows.json)
directly with the renderer; no database or client-data copy is needed.
The example template loops over `rows` and provides a zero-row state.

For real onboarding, prepare the relation, required grants, and safe validator
in the client repository. Apply them through the reviewed host workflow.
Validation should prove object shape, ownership, permissions, freshness, and
expected counts without exposing underlying client records. Query Engine's
artifact upsert does not create the Data relation or its grants.

Interactive artifacts may additionally define `{view_name}_query(jsonb)`
for filtering, sorting, pagination, and response construction. The client
database owns that logic; see the [service API reference](../README.md#interactive-artifact-data).

## 5. Prepare Web And Email Artifact Definitions

Use [web-artifact.json](examples/onboarding/web-artifact.json) as the complete
synthetic web definition. Its core contract is:

- `client_key` identifies the metadata client and authenticated client scope.
- `artifact_key` and `template_key` are stable client-owned identities.
- `view_name` and `template` are both required for a direct-source web artifact.
- `template.html_content` contains Jinja2 HTML; Query Engine HTML-escapes values.
- `delivery_mode: "web"` defines a display artifact.

Use [email-artifact.json](examples/onboarding/email-artifact.json) for a
separate email wrapper. It has `delivery_mode: "email"`, one `body` reference
to `weekly-summary`, and no direct `view_name` or `template`.
The recipient is deliberately non-deliverable; never use this fixture for a
real send.

The request shape implemented by Query Engine is:

```http
POST /artifacts
Authorization: Bearer <SIGNED_INTERNAL_TOKEN_FOR_CLIENT>
Content-Type: application/json

<complete artifact definition from the example JSON file>
```

This is a service API contract example. Actual environment artifact changes
must use the current governed artifact/metadata workflow and its reviewed
payloads. Do not substitute a direct API write for a required promotion.

`POST /artifacts` upserts client, template, and artifact metadata. It
**replaces the artifact's static recipients and references** with the supplied
lists; omitted lists default to empty. Review the complete intended definition
before an update. Authentication, authorization grants, data objects, home
cards, and schedules require their own reviewed declarations.

Static recipient configuration is the simplest example. Configured
recipient-source views and distribution groups are also supported by the
service; use the approved audience contract described in the
[API reference](../README.md). Distribution membership does not grant artifact
read or execute access.

## 6. Validate Display And Dry-Run Separately

Confirm service health and the exact installed runtime before artifact checks.
`GET /health` alone does not prove that the report, authorization, or delivery
configuration works.

| Check | Request contract | Expected evidence |
| --- | --- | --- |
| Normal display | `GET /artifacts/example/weekly-summary` | Authenticated HTML; expected layout and zero-row state |
| Display execution | `POST /artifact-executions` with [display-execution.json](examples/onboarding/display-execution.json) | Run result and `preview_html`; no email |
| Email dry-run | `POST /artifact-executions` with [dry-run-execution.json](examples/onboarding/dry-run-execution.json) | Render/run evidence; no email |
| Authorization | Authorized and denied audiences through the environment's actual ingress | Correct client/artifact scope, including required assets |
| Data contract | Client-owned reviewed validator | Safe schema, permission, count, and freshness results |

Protected service routes require a valid signed internal bearer token whose
`client_key` matches the requested client. Display and execution also require
the authenticated subject and permitted artifact scope. Use the environment's
existing Security/ingress path; do not mint example tokens or bypass it.

An execution request should always specify `behavior`; the API default is
`deliver`. `display` and `dry-run` avoid sending, but still render data and
write execution evidence. Real rendered responses require human review or
non-exposing validation; synthetic fixture HTML may be inspected locally.

PDF is an optional execution output. CSV requires the artifact's database-owned
query contract and an execution `query`. Onboard either only when required by
the client artifact contract; see the API reference for their exact semantics.

## 7. Enable Delivery And Scheduling When Requested

A successful dry-run does not authorize a live send. Resolve the approved
sending identity, exact test audience, artifact scope, environment, and
delivery window before enabling delivery or a schedule. Real recipients stay
in the approved configuration; do not put them into these shared examples.

For an authorized delivery, the execution shape is the dry-run request with
`behavior: "deliver"` and the approved client's artifact identities.
Delivery may return a queued run; track its `run_id` through
`GET /artifact-executions/{run_id}`. Do not blindly repeat a send after a
timeout or unknown delivery status.

`provider_accepted` means the provider accepted the message. It is not
confirmed delivery; the service's reconciliation contract distinguishes those
states. Scheduling belongs to the owning scheduler/DI workflow and must retain
the environment's declared schedule ownership.

## 8. Troubleshoot From Safe Evidence

| Symptom | First check |
| --- | --- |
| Unauthorized or denied request | Installed Security contract, token client scope, subject, current roles, artifact permission, and ingress route |
| Missing render relation | Schema-qualified name, migration state, connection binding, and Query Engine's object grants |
| Wrong columns or empty output | Reviewed data-contract validator and template's zero-row behavior |
| Delivery rejected or unresolved | Run/delivery status and reviewed non-exposing Email Service/credential-helper diagnostics |
| Old display content | Runtime/source identity, report freshness, and the environment's cache contract |

Use status summaries and reviewed diagnostics. Do not copy raw service logs,
recipient rows, client report HTML, or secret values into the onboarding record.
A fix to service runtime, client content, authorization, or environment state
must return to the workflow that owns that change.

## 9. Record Completion In The Owning Repositories

Onboarding is complete when the required environment and artifact checks pass
and the durable records identify:

- client and environment, owner, and approved audience;
- client source repo/branch/commit and pinned service runtime;
- artifact/template identities, routes, and data contract;
- exact package/release and upstream acceptance when applicable;
- safe validation evidence and any pending delivery or schedule scope;
- deployed-state and authorization-state inventory updates;
- monitoring registration/validation for a new environment;
- the next action for any unresolved work.

Use the orchestration
[deployment inventory](https://github.com/BicycleBi/bci-container-orch/blob/main/docs/inventory/deployment-inventory.md),
[authorization inventory](https://github.com/BicycleBi/bci-container-orch/blob/main/docs/inventory/authorization-access-inventory.md),
and monitoring
[client-stack contract](https://github.com/BicycleBi/bci-monitoring/blob/main/docs/client-stack-monitoring.md).
Client-specific decisions and handoffs belong in the client repo. Keep this
guide and its examples reusable.

For reusable Codex instructions, continue with
[AI Client Setup Prompts](ai-client-setup-prompts.md).
