# bci-query-engine

Postgres-driven HTML report renderer and artifact runner for Bicycle Curated Intelligence.

## What it does

1. Reads artifact config from the `metadata` Postgres database (`app.*` tables)
2. Executes the artifact's named view against the `data` Postgres database
3. Renders an HTML report via Jinja2
4. Delivers the report based on `delivery_mode`:
   - `email` — POSTs to `bci-email-service` which sends via Microsoft Graph
   - `web`   — returns rendered HTML for front-end consumption
   - `both`  — email + web
5. Logs every run to `log.artifact_runs`

Redis cache support is optional and disabled by default. When enabled, Query
Engine caches rendered HTML only for display/preview paths. Delivery and
dry-run executions still run through Postgres and the normal render path every
time so Redis never becomes the source of truth and never owns side effects.

If Redis is unavailable, Query Engine logs a warning and falls back to the
normal Postgres-backed execution path.

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
  "behavior": "deliver"
}
```

Supported behaviors:
- `deliver` — render and deliver if the artifact metadata allows it
- `display` — render and log while returning HTML in `preview_html`
- `dry-run` — render and log without sending

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
| `SECURITY_TOKEN_SECRET` | Shared signing secret for internal BCI bearer tokens accepted on protected query-engine routes |
| `QUERY_ENGINE_SECURITY_TOKEN_SECRET` | Optional query-engine-specific override for `SECURITY_TOKEN_SECRET` |
| `SECURITY_TOKEN_ISSUER` | Expected internal token issuer (default: `bci-security`) |
| `QUERY_ENGINE_SECURITY_TOKEN_ISSUER` | Optional query-engine-specific override for `SECURITY_TOKEN_ISSUER` |
| `SECURITY_TOKEN_AUDIENCE` | Expected internal token audience (default: `bci-client`) |
| `QUERY_ENGINE_SECURITY_TOKEN_AUDIENCE` | Optional query-engine-specific override for `SECURITY_TOKEN_AUDIENCE` |
| `REDIS_ENABLED` | Enables Redis-backed render caching when `true` (default: `false`) |
| `REDIS_HOST` | Redis host (default: `redis`) |
| `REDIS_PORT` | Redis port (default: `6379`) |
| `REDIS_DB` | Redis logical database number (default: `0`) |
| `REDIS_PASSWORD` | Redis password. Prefer `REDIS_PASSWORD_FILE` for container secrets. |
| `REDIS_PASSWORD_FILE` | Path to a file containing the Redis password |
| `REDIS_SSL` | Enables TLS for Redis when `true` (default: `false`) |
| `REDIS_SSL_CA_CERTS` | Optional CA certificate path for Redis TLS |
| `REDIS_CONNECT_TIMEOUT_SECONDS` | Redis connect timeout in seconds (default: `2`) |
| `REDIS_SOCKET_TIMEOUT_SECONDS` | Redis socket timeout in seconds (default: `2`) |
| `CACHE_TTL_SECONDS` | Render cache TTL in seconds (default: `3600`) |
| `CACHE_RENDERED` | Enables rendered HTML cache reads/writes when Redis is enabled (default: `true`) |
| `PORT` | Port to listen on (default: 8300) |

See `.env.example` for a complete list.

Protected artifact routes require a valid signed internal token whose
`client_key` claim matches the requested artifact client. Tokens with a missing
or mismatched `client_key` are rejected before artifact write, render, or
execution logic runs.

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
