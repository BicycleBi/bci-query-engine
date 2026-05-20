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
5. Logs every run to `log.artifact_runs`

## HTTP API

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/run/{client_key}/{artifact_key}?mode=email\|preview\|dry-run` | Trigger a run |
| `GET`  | `/run/{run_id}` | Get run status |
| `GET`  | `/health` | Health check |

### `POST /run/{client_key}/{artifact_key}`

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
| `METADATA_DB_PASSWORD` | Postgres password |
| `DATA_DB_HOST` | Postgres host (usually same as METADATA) |
| `DATA_DB_PORT` | Postgres port |
| `DATA_DB_NAME` | Data database name |
| `DATA_DB_USER` | Postgres user |
| `DATA_DB_PASSWORD` | Postgres password |
| `EMAIL_SERVICE_URL` | Base URL of bci-email-service (default: `http://email-service:8200`) |
| `PORT` | Port to listen on (default: 8300) |

See `.env.example` for a complete list.

## Running locally (with compose)

```bash
# from bci-container-orch
cd stacks/query-engine-testing
podman compose up --build
```

## Sibling repos required

```
../bci-postgres-service/
../bci-email-service/
../vault-credentials/
```
