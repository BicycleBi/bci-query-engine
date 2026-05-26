# Client Onboarding Manual

This manual describes how to set up a new BCI client using the current container model.
It is written for operators setting up a client-specific Postgres container, query-engine,
email-service, and artifact metadata.

The current proven reference implementation is `stacks/srp-local`. For a new client,
use that stack as the working template until a generic `stacks/client-template` exists.

## Current Model

Each client gets its own isolated runtime stack:

1. Postgres container
   - `metadata` database for app config, security metadata, and logs
   - `data` database for client data and report views
2. credential-helper container
   - retrieves Graph/email credentials from Vaultwarden
3. email-service container
   - sends rendered HTML through Microsoft Graph
   - writes delivery audit rows to `metadata.log.bci_email_delivery`
4. query-engine container
   - reads artifact config from `metadata.app.*`
   - reads report data from views in `data.public`
   - renders HTML
   - calls email-service for delivery
   - writes run rows to `metadata.log.artifact_runs`

The query-engine is client-agnostic. Client-specific behavior comes from database
metadata and the client data views.

## Before You Start

Choose these client values first:

| Value | Example | Notes |
| --- | --- | --- |
| Client key | `abc` | Short lowercase identifier. Used in API requests and metadata. |
| Client display name | `ABC Health` | Human-readable label. |
| Stack name | `bci-abc-local` | Docker Compose project name. |
| Data database | `data` | Current standard. |
| Metadata database | `metadata` | Current standard. |
| Postgres port inside Docker | `62345` | Current standard. No host port required. |
| Query-engine host port | `18300` | Pick a free localhost port per stack. |
| Email-service host port | `18200` | Pick a free localhost port per stack. |
| Credential-helper host port | `18100` | Pick a free localhost port per stack. |

Required sibling repos:

```text
bci-container-orch/
bci-postgres-service/
bci-query-engine/
bci-email-service/
vault-credentials/
```

## Secrets And Vault

The `.env` file for the stack is local-only and must not be committed.

Required `.env` values:

| Variable | Purpose |
| --- | --- |
| `SERVICE_TOKEN` | Shared internal bearer token. Query-engine sends it to email-service; email-service sends it to credential-helper. |
| `POSTGRES_PASSWORD` | Password for the client Postgres container. |
| `BW_CLIENTID` | Vaultwarden API client ID. |
| `BW_CLIENTSECRET` | Vaultwarden API client secret. |
| `BW_PASSWORD` | Vaultwarden master password. |
| `BW_SERVER_URL` | Vaultwarden URL. |
| `GRAPH_APP_VAULT_ITEM` | Vault item containing Graph app config. |
| `GRAPH_CERT_VAULT_ITEM` | Vault item containing Graph certificate credential. |

Generate local secrets:

```bash
openssl rand -hex 32   # SERVICE_TOKEN
openssl rand -hex 16   # POSTGRES_PASSWORD
```

The email-service must use:

```text
GRAPH_CONFIG_SOURCE=credential_service
CREDENTIAL_SERVICE_URL=http://credential-helper:8100
POSTGRES_CONFIG_SOURCE=env
POSTGRES_SCHEMA=log
```

`POSTGRES_SCHEMA=log` matters because the delivery audit table is
`metadata.log.bci_email_delivery`.

## Create The Client Stack

Until a generic template exists, copy the SRP stack:

```bash
cd /Users/Daniel/GIT-Repos/bci-container-orch
cp -R stacks/srp-local stacks/abc-local
```

Then update `stacks/abc-local/docker-compose.yml`:

1. Change the top-level compose `name`.
2. Keep the internal Postgres port as `62345`.
3. Choose unique localhost ports for exposed services.
4. Keep query-engine on the same internal Docker network as Postgres and email-service.
5. Keep query-engine `EMAIL_SERVICE_URL=http://email-service:8200`.
6. Keep email-service `POSTGRES_SCHEMA=log`.

Copy and fill the environment file:

```bash
cp stacks/abc-local/.env.example stacks/abc-local/.env
```

Start the stack:

```bash
cd stacks/abc-local
docker compose up -d --build postgres credential-helper email-service query-engine
```

Health checks:

```bash
curl http://127.0.0.1:18300/health
curl http://127.0.0.1:18200/health
curl http://127.0.0.1:18100/health
```

Use the ports chosen for the new client.

## Database Setup

The Postgres image initializes the standard metadata and data structure from
`bci-postgres-service/initdb`:

| Database | Purpose |
| --- | --- |
| `metadata` | `app`, `security`, and `log` schemas |
| `data` | Client data tables and reporting views |

The query-engine expects:

1. Client source tables loaded into `data`.
2. Report-ready views in `data.public`.
3. Artifact metadata in `metadata.app.*`.

For the current implementation, put client reporting views in `public`.
Example:

```sql
CREATE OR REPLACE VIEW public.weekly_visit_counts AS
SELECT
  location_name,
  visit_date,
  visit_count
FROM public.client_visit_source;
```

The `view_name` stored on a web artifact must include the schema:

```text
public.weekly_visit_counts
```

## Loading Client Data

The ingestion path depends on the client source.

Common options:

1. Restore a dump into `data`.
2. Load CSV/extract files into `data.public`.
3. Run a client-specific ETL container or script.

After loading data, create one or more stable reporting views. The query-engine reads
from views; it should not contain client-specific SQL logic.

Validation:

```bash
docker compose exec postgres \
  psql -U postgres -d data -p 62345 \
  -c "SELECT count(*) FROM public.weekly_visit_counts;"
```

## Artifact Setup

There are two useful artifact shapes today.

### Web Artifact

A web artifact owns the data source and template. It can be rendered through `GET`.

Required fields:

- `client_key`
- `artifact_key`
- `view_name`
- `delivery_mode`
- `template`

Example request:

```bash
curl -X POST http://127.0.0.1:18300/artifacts \
  -H 'Content-Type: application/json' \
  -d '{
    "client_key": "abc",
    "client_display_name": "ABC Health",
    "artifact_key": "weekly-visit-counts-page",
    "display_name": "Weekly Visit Counts",
    "description": "HTML page for weekly visit counts.",
    "view_name": "public.weekly_visit_counts",
    "delivery_mode": "web",
    "active": true,
    "template": {
      "template_key": "weekly-visit-counts-page",
      "version": 1,
      "display_name": "Weekly Visit Counts",
      "content_type": "html",
      "html_content": "<html><body><h1>Weekly Visit Counts</h1>{% for row in rows %}<p>{{ row.location_name }}: {{ row.visit_count }}</p>{% endfor %}</body></html>",
      "is_active": true
    },
    "recipients": []
  }'
```

Render it:

```bash
curl http://127.0.0.1:18300/artifacts/abc/weekly-visit-counts-page
```

### Email Wrapper Artifact

An email wrapper artifact references another artifact as its body. It does not need
its own `view_name` or template.

Required fields:

- `client_key`
- `artifact_key`
- `delivery_mode=email`
- `recipients`
- `references`

Example request:

```bash
curl -X POST http://127.0.0.1:18300/artifacts \
  -H 'Content-Type: application/json' \
  -d '{
    "client_key": "abc",
    "client_display_name": "ABC Health",
    "artifact_key": "weekly-visit-counts-email",
    "display_name": "Weekly Visit Counts Email",
    "description": "Email delivery wrapper for weekly visit counts.",
    "delivery_mode": "email",
    "active": true,
    "recipients": [
      {"email": "person@example.com", "delivery_type": "to", "active": true}
    ],
    "references": [
      {
        "referenced_artifact_key": "weekly-visit-counts-page",
        "reference_role": "body",
        "output_format": "html",
        "active": true
      }
    ]
  }'
```

Recipient `delivery_type` can be:

- `to`
- `cc`
- `bcc`

Posting an artifact definition is an upsert. For recipients and references, the current
implementation replaces the artifact's existing rows with the rows in the request.

## Query-Engine Requests

### Health

```bash
curl http://127.0.0.1:18300/health
```

### Save Or Update Artifact

```http
POST /artifacts
```

Use this for both web artifacts and email wrapper artifacts.

### Render For Display

```http
GET /artifacts/{client_key}/{artifact_key}
```

Example:

```bash
curl http://127.0.0.1:18300/artifacts/abc/weekly-visit-counts-page
```

### Execute Artifact

```http
POST /artifact-executions
```

Behaviors:

| Behavior | Result |
| --- | --- |
| `display` | Render HTML, log the run, and return `preview_html`. |
| `dry-run` | Render HTML and log the run without sending email. |
| `deliver` | Render and send email when `delivery_mode` is `email` or `both`. |

Example dry run:

```bash
curl -X POST http://127.0.0.1:18300/artifact-executions \
  -H 'Content-Type: application/json' \
  -d '{"client_key":"abc","artifact_key":"weekly-visit-counts-email","behavior":"dry-run"}'
```

Example live delivery:

```bash
curl -X POST http://127.0.0.1:18300/artifact-executions \
  -H 'Content-Type: application/json' \
  -d '{"client_key":"abc","artifact_key":"weekly-visit-counts-email","behavior":"deliver"}'
```

### Get Run Status

```http
GET /artifact-executions/{run_id}
```

Example:

```bash
curl http://127.0.0.1:18300/artifact-executions/00000000-0000-0000-0000-000000000000
```

Legacy routes still exist:

- `POST /run/{client_key}/{artifact_key}?mode=email`
- `POST /run/{client_key}/{artifact_key}?mode=dry-run`
- `POST /run/{client_key}/{artifact_key}?mode=preview`
- `GET /run/{run_id}`

Use the `/artifact-executions` routes for new work.

## Validate A Client Setup

Run these checks before considering a client ready.

### Containers

```bash
docker compose ps
```

Expected:

- Postgres healthy
- credential-helper healthy
- email-service healthy
- query-engine healthy

### Metadata

```bash
docker compose exec postgres \
  psql -U postgres -d metadata -p 62345 \
  -c "SELECT client_key, artifact_key, delivery_mode, active FROM app.artifacts ORDER BY artifact_key;"
```

```bash
docker compose exec postgres \
  psql -U postgres -d metadata -p 62345 \
  -c "SELECT a.artifact_key, r.email, r.delivery_type, r.active FROM app.artifact_recipients r JOIN app.artifacts a ON a.artifact_id = r.artifact_id ORDER BY a.artifact_key, r.email;"
```

### Data View

```bash
docker compose exec postgres \
  psql -U postgres -d data -p 62345 \
  -c "SELECT count(*) FROM public.weekly_visit_counts;"
```

### Display Render

```bash
curl -o /tmp/client-artifact.html \
  http://127.0.0.1:18300/artifacts/abc/weekly-visit-counts-page
```

### Dry Run

```bash
curl -X POST http://127.0.0.1:18300/artifact-executions \
  -H 'Content-Type: application/json' \
  -d '{"client_key":"abc","artifact_key":"weekly-visit-counts-email","behavior":"dry-run"}'
```

### Live Send

```bash
curl -X POST http://127.0.0.1:18300/artifact-executions \
  -H 'Content-Type: application/json' \
  -d '{"client_key":"abc","artifact_key":"weekly-visit-counts-email","behavior":"deliver"}'
```

### Logs

```bash
docker compose exec postgres \
  psql -U postgres -d metadata -p 62345 \
  -c "SELECT run_id, client_key, artifact_key, status, row_count, recipient_count, error_message, completed_at FROM log.artifact_runs ORDER BY started_at DESC LIMIT 10;"
```

```bash
docker compose exec postgres \
  psql -U postgres -d metadata -p 62345 \
  -c "SELECT delivery_id, to_recipients, status, status_code, provider_message_id, error_code, error_message, completed_at FROM log.bci_email_delivery ORDER BY created_at DESC LIMIT 10;"
```

Success for email means:

- query-engine execution returns `status: success`
- `log.artifact_runs.status = completed`
- `log.bci_email_delivery.status = sent`
- `log.bci_email_delivery.status_code = 202`

## Troubleshooting

### Query-engine returns Postgres password authentication failed

The Docker volume may have been initialized with an older Postgres password. Either:

1. recreate the volume, or
2. rotate the local database password to match the current `.env`.

For local development, recreating the volume is often simpler:

```bash
docker compose down -v
docker compose up -d --build postgres credential-helper email-service query-engine
```

Only use `down -v` when it is acceptable to delete the local Postgres data volume.

### Email-service returns 401 or 403

Check that `SERVICE_TOKEN` is the same for query-engine, email-service, and credential-helper.

### Email-service says `public.bci_email_delivery` does not exist

Set email-service `POSTGRES_SCHEMA=log`.

### Delivery hangs or times out while fetching credentials

Check credential-helper health and logs:

```bash
docker compose logs --tail=100 credential-helper
```

Also confirm the Vault item names in `.env`.

### Query-engine cannot find the data view

Check that the view exists in the `data` database and that the artifact `view_name`
uses the schema-qualified name, for example `public.weekly_visit_counts`.

## Current Limits

The following are not fully implemented yet:

- query-driven or burst-recipient resolution
- attachments such as PDF, XLSX, CSV, or TXT
- scheduler-driven executions
- security service and user-facing authentication
- generic client stack scaffolding
- production hardening around retries and delivery controls

For now, production-like sends should be triggered manually through
`POST /artifact-executions` after display and dry-run validation.
