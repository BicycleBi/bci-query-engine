# Codex Handoff

Date: 2026-05-25
Repo: bci-query-engine

## Scope

This work spanned three repos:

1. bci-query-engine
2. bci-postgres-service
3. bci-container-orch

The goal was to move query-engine toward a metadata-driven artifact model with artifact references, a REST-style API surface, and an SRP local stack for integration testing.

## Design Decisions Locked In

1. Artifact owns the data source.
2. Template is presentation only.
3. Delivery is a function over an artifact, not a separate core artifact type.
4. Logging lives in the metadata DB.
5. Normal display retrieval is GET.
6. Writes and execution creation are POST.
7. Behavior should come from metadata, not route names or query params.

## What Changed

### bci-query-engine

1. Added design docs:
   - docs/query-engine-blueprint.md
   - docs/query-engine-high-level.md
2. Added artifact write support and artifact reference support.
3. Added REST-style endpoints:
   - POST /artifacts
   - GET /artifacts/{client_key}/{artifact_key}
   - POST /artifact-executions
   - GET /artifact-executions/{run_id}
4. Kept legacy /run endpoints as deprecated compatibility aliases.
5. Added SRP sample assets:
   - templates/srp_visit_counts_quick.html
   - scripts/seed_srp_quick_artifact.py
   - scripts/seed_srp_quick_email_artifact.py
6. Added email-service bearer token support in app/mailer.py so query-engine sends Authorization: Bearer $SERVICE_TOKEN to bci-email-service.

Key files:

1. app/main.py
2. app/engine.py
3. app/models.py
4. app/mailer.py

### bci-postgres-service

1. Added metadata schema bootstrap for app, security, and log.
2. Added initial metadata tables for clients, templates, artifacts, recipients, schedules, security, and run/email logs.
3. Added artifact reference support.
4. Allowed wrapper artifacts by making app.artifacts.view_name nullable.

### bci-container-orch

1. Added stacks/srp-local.
2. Added compose, env example, and restore script.
3. Restore flow loads the SRP dump into the data DB and removes unwanted burst-control tables from public.

## Seeded SRP Artifacts

1. srp / visit-counts-quick-page
   - owns the SRP data source
   - renders the quick HTML page
2. srp / visit-counts-quick-email
   - delivery wrapper artifact
   - references visit-counts-quick-page as body
   - static recipients are daniel@bicyclebi.com and jeanre@bicyclebi.com

## What Was Validated

1. Metadata schemas and tables were created and verified.
2. SRP local stack was restored and healthy.
3. visit-counts-quick-page was seeded and successfully rendered.
4. visit-counts-quick-email was converted into a reference-based email artifact pointing to the page artifact.
5. The new REST routes were validated against live SRP metadata/data for display retrieval and dry-run execution.
6. All three repos were committed and pushed.

## Pushed Commits

### bci-query-engine

1. e9f7a94 - Add artifact references and execution endpoints
2. f2de20a - Add email service auth token support

### bci-postgres-service

1. 3c0df7b - Bootstrap query engine metadata schemas

### bci-container-orch

1. b9e3ed9 - Add SRP local restore stack

## Email Delivery Status

Real end-to-end email delivery has now been confirmed through the SRP local stack.

What is known:

1. A live POST /artifact-executions delivery call originally failed with 401 Unauthorized from http://email-service:8200/send.
2. That was traced to query-engine not sending the shared SERVICE_TOKEN.
3. Query-engine was patched to send the bearer token correctly.
4. The SRP local orchestrator stack now runs query-engine alongside Postgres, credential-helper, and email-service.
5. Email-service audit logging is configured for log.bci_email_delivery.
6. A live delivery call returned success and Graph accepted the send with HTTP 202.

Current state:

1. Auth bug identified and fixed.
2. Query-engine can render the SRP page artifact from live restored data.
3. Query-engine can call email-service with the shared bearer token.
4. Email-service can retrieve Graph credentials through the credential helper.
5. Email-service can send via Microsoft Graph and write the delivery audit row.

## Local Test Flow

1. Bring up the SRP local stack from bci-container-orch/stacks/srp-local.
2. Confirm query-engine health at http://127.0.0.1:18300/health.
3. Trigger POST /artifact-executions with:

```json
{
  "client_key": "srp",
  "artifact_key": "visit-counts-quick-email",
  "behavior": "deliver"
}
```

4. Confirm:
   - query-engine returns 202 with status success
   - log.artifact_runs has a completed run
   - log.bci_email_delivery records the delivery with status sent and status_code 202
   - the email lands with the configured recipients
5. If delivery still fails, inspect bci-email-service logs and log.bci_email_delivery for the next downstream failure.

## Known Gaps

1. Burst or query-driven recipient resolution is not implemented yet.
2. Attachment outputs beyond HTML body are still future work.
3. Legacy /run endpoints still exist and should eventually be removed once callers switch over.
