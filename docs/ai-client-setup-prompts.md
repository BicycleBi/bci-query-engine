# Client-Agnostic AI Onboarding Prompts

Use these prompts with [Client-Agnostic Query Engine Onboarding](client-onboarding-manual.md).
Fill in the approved inputs for the client; the shared service examples use
only fictional identities and synthetic data.

The owning repositories and installed workflow determine source, environment,
authorization, and execution requirements. These prompts do not authorize a
deployment, database mutation, direct artifact API write, or live email send.

## 1. Assess And Plan Onboarding

```text
Prepare a Query Engine onboarding plan for:
Client key and display name:
Owning client repo:
Environment:
New environment or existing environment:
Artifact purpose and delivery channels:
Approved data classification and source contract:
Audience and authorization requirements:
Refresh/delivery owner:

Read bci-query-engine/docs/client-onboarding-manual.md, the client repo's
AGENTS.md and relevant handoffs, and the current bci-container-orch development,
initial-stack/deployment, and data-safety rules.
Resolve missing non-secret inputs from repository evidence.
Separate environment setup, client data contracts, artifact metadata/templates,
authorization, delivery, monitoring, and inventory work by owning workflow.
Use the generic orchestration setup contract for a new environment.
Produce the proposed sequence and concrete files; do not clone another client's
stack or assume its ports, roles, report schema, or recipient configuration.
```

## 2. Prepare Environment Inputs

```text
Prepare the non-secret environment inputs for:
Client key:
Environment and technical key:
Reviewed host/runtime allocation:
Hostname and identity-provider readiness:
Existing or new boundary:
Required service/source revisions:

For a new boundary, follow the current initial-client-stack-setup runbook and
its generic setup-spec example. Prepare the exact source/support files and
read-only preflight inputs. Follow the runbook's separate administrator apply
authority; do not substitute a raw Compose command.
For an existing boundary, verify its inventory and current workflow rather
than rerunning bootstrap.
Record only secret references and operator-confirmed readiness, never values.
```

## 3. Prepare The Data And Rendering Contract

```text
Prepare the client-owned Query Engine data contract for:
Client key:
Artifact key:
Approved schema-qualified render relation:
Output grain and columns:
Data classification:
Refresh owner and freshness requirement:

Keep client-specific SQL, migrations, grants, templates, and validators in the
client repository. Use the approved secured-host Dev stack for database-backed
work; do not copy client databases or client data to a developer machine.
Use synthetic fixtures for local rendering checks.
Prepare reviewed non-exposing validation for schema, permissions, counts,
freshness, and contract readiness. Do not inspect raw client rows, report
payloads, recipient details, or secrets.
Follow stricter repository rules and the controlling data-safety policy.
```

## 4. Prepare Artifact Definitions And Navigation

```text
Prepare the artifact package for:
Client key and display name:
Artifact/template key:
Render relation:
Template source:
Web/email channels:
Approved audience/reference to delivery configuration:
Homepage impact:

Use Query Engine's current ArtifactWriteRequest model and the synthetic
examples in docs/examples/onboarding as contract references.
A direct web artifact needs both view_name and template.
An email wrapper may reference the web artifact as its single body source.
Review full recipients/references lists: artifact upsert replaces those lists,
including empty defaults when omitted.
Prepare source payloads for the governing artifact/metadata workflow.
Do not automatically POST to a running service.
Preserve unrelated home cards, permissions, artifacts, and schedules.
```

## 5. Validate Without Sending

```text
Prepare and run the permitted validation for:
Client key and environment:
Artifact identities:
Exact source/package/runtime:
Approved authorization audiences:

Use local static/model validation and synthetic template rendering first.
For live checks, follow the owning host workflow and current data-safety rules.
Specify behavior=display or behavior=dry-run explicitly; omitted behavior
defaults to deliver. A dry-run still renders data and logs a run.
Verify authorized access, expected denial, asset loading, layout, zero-row
state, and safe data-contract results.
Keep real report HTML, client values, recipient identities, and secrets out
of AI-visible outputs. Use human browser review or non-exposing checks.
Record pass/fail evidence and blockers. Do not advance to live sending.
```

## 6. Prepare Authorized Delivery Or Scheduling

```text
Prepare delivery/schedule work for this explicitly requested scope:
Client key and environment:
Artifact:
Approved sending identity:
Approved test audience reference:
Delivery window or schedule:
Acceptance and current workflow authority:

Resolve the current delivery and scheduling contracts from the owning
repositories. Preserve exact recipients and environment boundaries.
Proceed only through already-authorized workflow steps; present a genuine
workflow authorization gate once with the complete reviewable scope.
Track a queued delivery by run_id. Do not blindly retry a timeout or unknown
send. Distinguish provider_accepted from trace-confirmed delivered.
Record the outcome without recipient details or message contents.
```

## 7. Diagnose Or Close Out

```text
Diagnose or close out Query Engine onboarding for:
Client key and environment:
Artifact and release/package:
Safe validation results or failed check:
Outstanding scope:

Inspect repository context and permitted status/metadata evidence.
Use reviewed non-exposing diagnostics; do not dump client data or raw logs.
Route a correction to the workflow that owns it.
Record client-specific scope, exact source/runtime, validation, inventories,
monitoring completion, pending work, and restart instructions in the owning
client/orchestration records.
Commit and push coherent source documentation on its correctly routed branch.
Do not push or merge a protected branch merely because onboarding was requested.
Keep the shared Query Engine guide and examples client-agnostic.
```
