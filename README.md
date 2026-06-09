# bci-query-engine

Postgres-driven HTML report renderer and artifact runner for Bicycle Curated Intelligence.

## What it does

1. Reads artifact config from the `metadata` Postgres database (`app.*` tables)
2. Executes the artifact's named view against the `data` Postgres database
3. Renders an HTML report via Jinja2
4. Delivers the report based on `delivery_mode`:
   - `email` — POSTs to `bci-email-service` which sends via Microsoft Graph
   - `web`   — returns rendered HTML for front-end consumption (caching deferred)
   - `both`  — email + web
5. Optionally generates requested file outputs such as PDF
6. Logs every run to `log.artifact_runs` and generated files to
   `log.artifact_outputs`

## HTTP API

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/artifacts` | Create or update an artifact definition in metadata |
| `GET`  | `/artifacts/{client_key}/{artifact_key}` | Render and return artifact HTML |
| `POST` | `/artifact-executions` | Create an artifact execution |
| `GET`  | `/artifact-executions/{run_id}` | Get artifact execution status |
| `GET`  | `/health` | Health check |

Legacy compatibility routes still exist for `/run/{client_key}/{artifact_key}` and `/run/{run_id}`, but they are no longer the primary API surface.

### `POST /artifacts`

Creates or updates the metadata-backed definition for a client artifact.

Current phase support includes:

- client upsert
- template upsert
- artifact upsert
- static recipient replacement for the artifact
- artifact body/attachment references in metadata

### `GET /artifacts/{client_key}/{artifact_key}`

Renders the artifact and returns HTML on the normal display path.

### `POST /artifact-executions`

Creates an execution request for an artifact.

**Request body:**
```json
{
  "client_key": "srp",
  "artifact_key": "visit-counts-quick-email",
  "behavior": "deliver",
  "output_formats": []
}
```

Supported behaviors:
- `deliver` — render and deliver if the artifact metadata allows it
- `display` — render and log while returning HTML in `preview_html`
- `dry-run` — render and log without sending

Supported optional output formats:
- `pdf` — render one PDF per data row returned by the artifact view

When PDF output is requested, Query Engine renders the artifact template once
per returned data row and writes files under `ARTIFACT_OUTPUT_DIR`, defaulting
to `/tmp/bci-query-engine/artifact-outputs`. Filenames use the render date, not
the report data as-of date. Generated file metadata is written to
`log.artifact_outputs` and returned in the execution response.

Example PDF display execution:

```bash
curl -X POST http://127.0.0.1:18300/artifact-executions \
  -H 'Content-Type: application/json' \
  -d '{
    "client_key": "srp",
    "artifact_key": "srp-practice-publication",
    "behavior": "display",
    "output_formats": ["pdf"]
  }'
```

### Legacy `POST /run/{client_key}/{artifact_key}`

**Query params:**
- `mode=email` (default) — render + send (respects `delivery_mode`)
- `mode=preview` — render only, return HTML in response body, no log
- `mode=dry-run` — render + log, no send

**Response 202:**
```json
{
  "run_id": 1,
  "client_key": "acme",
  "artifact_key": "weekly-summary",
  "status": "success",
  "started_at": "2024-01-01T12:00:00Z",
  "completed_at": "2024-01-01T12:00:01Z"
}
```

## Environment variables

| Variable | Description |
|----------|-------------|
| `METADATA_DB_HOST` | Postgres host |
| `METADATA_DB_PORT` | Postgres port (default: 5432) |
| `METADATA_DB_NAME` | Metadata database name |
| `METADATA_DB_USER` | Postgres user |
| `METADATA_DB_PASSWORD` | Postgres password. Prefer `METADATA_DB_PASSWORD_FILE` for container secrets. |
| `METADATA_DB_PASSWORD_FILE` | Path to a file containing the Postgres password |
| `DATA_DB_HOST` | Postgres host (usually same as METADATA) |
| `DATA_DB_PORT` | Postgres port |
| `DATA_DB_NAME` | Data database name |
| `DATA_DB_USER` | Postgres user |
| `DATA_DB_PASSWORD` | Postgres password. Prefer `DATA_DB_PASSWORD_FILE` for container secrets. |
| `DATA_DB_PASSWORD_FILE` | Path to a file containing the Postgres password |
| `EMAIL_SERVICE_URL` | Base URL of bci-email-service (default: `http://email-service:8200`) |
| `EMAIL_SERVICE_TIMEOUT_SECONDS` | Timeout for bci-email-service requests in seconds (default: `90`) |
| `SERVICE_TOKEN` | Shared bearer token used for internal calls to bci-email-service |
| `PORT` | Port to listen on (default: 8300) |
| `ARTIFACT_OUTPUT_DIR` | Directory where generated file outputs are written. Defaults to `/tmp/bci-query-engine/artifact-outputs`. |
| `PDF_CHROMIUM_EXECUTABLE` | Optional path/name for the Chromium executable used for PDF rendering. |
| `PDF_RENDER_TIMEOUT_SECONDS` | Timeout for a single PDF render. Defaults to `120`. |

See `.env.example` for a complete list.

## Running locally (with compose)

```bash
# from bci-container-orch
cd stacks/srp-local
docker compose up -d --build postgres credential-helper email-service query-engine
```

The SRP local stack exposes query-engine at `http://127.0.0.1:18300`.
The seeded `srp / visit-counts-quick-email` artifact sends to
`daniel@bicyclebi.com` and `jeanre@bicyclebi.com`.

```bash
curl -X POST http://127.0.0.1:18300/artifact-executions \
  -H 'Content-Type: application/json' \
  -d '{"client_key":"srp","artifact_key":"visit-counts-quick-email","behavior":"deliver"}'
```

## Sibling repos required

```
../bci-postgres-service/
../bci-email-service/
../bci-container-orch/
../vault-credentials/
```
