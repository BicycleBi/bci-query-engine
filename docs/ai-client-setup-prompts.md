# AI Client Setup Prompts

Use these prompts with Codex when setting up a new client. They are written to
keep the work aligned with the current implementation:

- client-specific Docker Compose stack
- Postgres `metadata` and `data` databases
- client data and report views in `data.public`
- artifact metadata created through query-engine `POST /artifacts`
- email delivery through query-engine -> email-service -> Microsoft Graph

Reference docs:

- [Client Onboarding Manual](client-onboarding-manual.md)
- [Database Technical Design](../../bci-container-orch/docs/database-design.md)
- [Email Service Technical Design](../../bci-container-orch/docs/email-service-design.md)

## Prompt 1: Assess A New Client Setup

```text
We need to set up a new BCI client.

Client key:
Client display name:
Desired stack folder:
Available source data:
Expected artifact:
Expected recipients:

Please inspect bci-container-orch, bci-query-engine, bci-postgres-service,
and bci-email-service. Confirm what already exists and produce an exact setup
checklist for this client.

Use docs/client-onboarding-manual.md as the source of truth. Do not introduce
new architecture unless the current implementation cannot support the need.
Call out any missing information before changing files.
```

## Prompt 2: Create The Client Stack

```text
Create a new local client stack from stacks/srp-local.

Client key:
Client display name:
New stack folder:
Compose project name:
Query-engine host port:
Email-service host port:
Credential-helper host port:

Update docker-compose.yml and .env.example for this client.
Keep Postgres internal-only on port 62345.
Keep email-service POSTGRES_SCHEMA=log.
Keep query-engine EMAIL_SERVICE_URL=http://email-service:8200.
Do not commit or print any real .env secrets.

After editing, validate docker compose config and show the commands needed to
start the stack.
```

## Prompt 3: Prepare The Client Database

```text
Prepare the data database for this client.

Client key:
Stack folder:
Source file or dump path:
Target source table names:
Required report-ready view names:

Use the current model: source data and reporting views live in the data database,
normally in the public schema. The query-engine must read schema-qualified views
such as public.weekly_report.

Create or update scripts needed to load the data and create stable reporting views.
Then provide validation SQL for row counts and sample records.
Do not expose Postgres on the host unless there is no other practical path.
```

## Prompt 4: Define A Web Artifact

```text
Create a web artifact definition for this client through query-engine.

Query-engine URL:
Client key:
Client display name:
Artifact key:
Display name:
Description:
Data view name:
Template file or HTML:

Build the POST /artifacts JSON body using the current query-engine schema.
The artifact should include view_name, delivery_mode=web, and a template.
Send the request if the query-engine is running, then validate that
GET /artifacts/{client_key}/{artifact_key} renders HTML.
```

## Prompt 5: Define An Email Artifact

```text
Create an email wrapper artifact for this client through query-engine.

Query-engine URL:
Client key:
Client display name:
Email artifact key:
Display name:
Description:
Referenced body artifact key:
Recipients:

Build the POST /artifacts JSON body using delivery_mode=email.
Do not include view_name or template on the email wrapper.
Add recipients with delivery_type to, cc, or bcc.
Add one body reference pointing to the web artifact.
Send the request if query-engine is running, then validate recipients in
metadata.app.artifact_recipients.
```

## Prompt 6: Run Display, Dry-Run, And Live Send

```text
Validate this client artifact end to end.

Stack folder:
Query-engine URL:
Client key:
Web artifact key:
Email artifact key:

Run these in order:
1. GET /health for query-engine and email-service.
2. GET /artifacts/{client_key}/{web_artifact_key}.
3. POST /artifact-executions with behavior=dry-run for the email artifact.
4. POST /artifact-executions with behavior=deliver for the email artifact.
5. Query metadata.log.artifact_runs.
6. Query metadata.log.bci_email_delivery.

Report run_id, recipient_count, delivery status, Graph status_code, and
provider_message_id. If delivery fails, inspect query-engine and email-service
logs and identify the exact failing boundary.
```

## Prompt 7: Update Documentation After Setup

```text
Update the documentation after setting up this client.

Client key:
Stack folder:
Artifacts created:
Data views created:
Validation results:

Update only docs that are useful to future operators. Keep docs aligned with the
current implementation. Remove or revise anything that suggests YAML runtime
artifact config, a report-service/frontend replacing query-engine, Redis as a
required local component, or direct Vault access from email-service.

Do not document secrets. Commit and push the final documentation changes.
```

## Prompt 8: Troubleshoot A Failed Send

```text
Troubleshoot a failed BCI email delivery.

Stack folder:
Query-engine URL:
Client key:
Artifact key:
Run ID if available:

Check the failure in this order:
1. query-engine response and logs
2. metadata.log.artifact_runs
3. email-service logs
4. metadata.log.bci_email_delivery
5. credential-helper logs
6. service token alignment
7. POSTGRES_SCHEMA for email-service
8. Vault item names and credential-helper health

Explain whether the failure is query-engine -> email-service auth, email-service
audit logging, credential retrieval, Microsoft Graph delivery, or artifact/data
configuration. Apply a fix only when the cause is clear.
```

## Prompt 9: Commit And Push Client Setup Work

```text
Commit and push the completed client setup work.

Repos involved:
Expected files changed:

Before committing:
1. Show git status for each repo.
2. Verify no .env files or secrets are staged.
3. Run available syntax/config checks.
4. Summarize live validation results.

Commit each repo with a clear message and push main.
After pushing, show final status and latest commit hashes.

```