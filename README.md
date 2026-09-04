# bci-query-engine

Postgres-driven HTML report renderer and artifact runner for Bicycle Curated Intelligence.

Query Engine is also the single persistence boundary for the shared Usage
Monitoring System. Security submits bounded login and authorization events to
`POST /internal/usage/events` using the existing service token. Nginx sends its
metadata-only timing log to the private UDP listener on port `8514`. Query
Engine validates both contracts before writing `monitoring.events` or
`monitoring.request_spans`; bodies, filters, credentials, cookies, tokens,
rendered content, and client-data values are not accepted.

The canonical schema is [docs/usage-monitoring-schema.sql](docs/usage-monitoring-schema.sql).

## What it does

1. Reads artifact config from the `metadata` Postgres database (`app.*` tables)
2. Executes the artifact's named view against the `data` Postgres database
3. Renders an HTML report via Jinja2
4. Delivers the report based on `delivery_mode`:
   - `email` — POSTs to `bci-email-service` which sends via Microsoft Graph
   - `web`   — returns rendered HTML for front-end consumption
   - `both`  — email + web
5. Optionally generates requested file outputs such as PDF and database-owned CSV
6. Logs every run to `log.artifact_runs` and generated files to
   `log.artifact_outputs`

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
| `GET`  | `/artifacts/{client_key}/{artifact_key}/assets/{asset_path}` | Return a versioned package-owned static asset |
| `GET`  | `/artifacts/{client_key}/{artifact_key}/distribution-groups` | List approved active distribution groups without recipient details |
| `GET`  | `/artifacts/{client_key}/{artifact_key}/usage-summary?days=30` | Return a bounded client-scoped usage and response-time summary |
| `POST` | `/artifact-executions` | Create an artifact execution |
| `GET`  | `/artifact-executions/{run_id}` | Get artifact execution status |
| `GET`  | `/health` | Health check |

Legacy compatibility routes still exist for `/run/{client_key}/{artifact_key}` and `/run/{run_id}`, but they are no longer the primary API surface.

The usage summary route is nested below the `usage-monitoring-dashboard`
artifact so the gateway applies the dashboard's artifact-read authorization.
It accepts only `7`, `30`, or `90` day windows and applies the same authenticated
client boundary as artifact routes. It returns aggregate
request, artifact, login, interaction, and response-time measures plus the
recorded display name/username for active users. It does not return email
addresses, request IDs, session IDs, raw events, request or response bodies,
filters, payloads, or rendered output.

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

### `GET /artifacts/{client_key}/{artifact_key}/distribution-groups`

Returns only active groups bound to the artifact that contain at least one
active recipient. The response contains bounded group keys, display names, and
descriptions; recipient identities, addresses, delivery types, and counts are
not exposed. The same artifact and client authorization boundary used by other
authenticated artifact routes applies.

### `POST /artifact-executions`

Creates an execution request for an artifact.

For `deliver`, Query Engine first persists a `queued` run and returns HTTP 202
with its `run_id`. Rendering and delivery then continue outside the request.
Poll `GET /artifact-executions/{run_id}` for `queued`, `preparing`, `sending`,
`completed`, or `failed` status. This keeps credential retrieval and provider
delivery latency out of the browser request lifecycle. Queued deliveries use a
bounded in-process worker pool (two workers by default; configurable with
`ARTIFACT_DELIVERY_WORKERS`) so a multi-artifact trigger cannot fan out every
delivery at once. The default Query Engine-to-Email Service timeout is 390
seconds so two sequential cold credential lookups can complete; deployments
can override it with `EMAIL_SERVICE_TIMEOUT_SECONDS`.

**Request body:**
```json
{
  "client_key": "srp",
  "artifact_key": "visit-counts-quick-email",
  "behavior": "deliver",
  "output_formats": [],
  "query": null,
  "distribution_group_keys": []
}
```

Supported behaviors:
- `deliver` — render and deliver if the artifact metadata allows it
- `display` — render and log while returning HTML in `preview_html`
- `dry-run` — render and log without sending

Supported optional output formats:
- `pdf` — render one PDF per data row returned by the artifact view
- `csv` — write one UTF-8 CSV from the artifact query function's database-owned
  `csv` contract

When PDF output is requested, Query Engine renders the artifact template once
per returned data row and writes files under `ARTIFACT_OUTPUT_DIR`, defaulting
to `/tmp/bci-query-engine/artifact-outputs`. Filenames use the render date, not
the report data as-of date. Generated file metadata is written to
`log.artifact_outputs` and returned in the execution response.

If `behavior` is `deliver` and the artifact delivery mode allows email,
generated PDF outputs are sent to email-service as attachments on the delivery
request.

CSV execution requires `query`. Query Engine invokes the artifact's existing
`{view_name}_query(jsonb)` function and accepts only this bounded result shape:

```json
{
  "csv": {
    "filename": "filtered-results.csv",
    "columns": [{"key": "field", "label": "Field"}],
    "rows": [{"field": "value"}]
  }
}
```

The database owns filtering, ordering, column selection, and row production.
Query Engine owns CSV encoding, spreadsheet-formula neutralization, size/row
limits, output logging, and attachment handoff. CSV execution queries bypass
render caching because delivery is a side effect and the filtered file must be
generated from current database results.

`distribution_group_keys` selects reusable application distribution groups for
a delivery execution. Every selected active group must be actively bound to
the artifact in `app.artifact_distribution_groups`. Active contacts are joined
through `app.distribution_group_members`, deduplicated case-insensitively, and
resolved with `to` taking precedence over `cc`, then `bcc`. Distribution
membership never grants artifact read or execute authorization.

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
| `ARTIFACT_OUTPUT_DIR` | Directory where generated file outputs are written. Defaults to `/tmp/bci-query-engine/artifact-outputs`. |
| `PDF_CHROMIUM_EXECUTABLE` | Optional path/name for the Chromium executable used for PDF rendering. |
| `PDF_RENDER_TIMEOUT_SECONDS` | Timeout for a single PDF render. Defaults to `120`. |
| `CSV_OUTPUT_MAX_ROWS` | Maximum rows accepted from a database-owned CSV contract. Defaults to `100000`. |
| `CSV_OUTPUT_MAX_BYTES` | Maximum generated CSV bytes. Defaults to `3145728` (3 MiB). |

See `.env.example` for a complete list.

Protected artifact routes require a valid signed internal token whose
`client_key` claim matches the requested artifact client. Tokens with a missing
or mismatched `client_key` are rejected before artifact write, render, or
execution logic runs.

For data views that implement authenticated scoping, Query Engine also requires
the token `sub` claim. For Nginx-protected requests, Security refreshes
Metadata roles and Nginx overwrites `X-Identity-Roles` with that authenticated
response before forwarding internally. Query Engine uses that fresh role set,
falling back to the signed token roles for direct service calls, and binds the
normalized context to the data transaction with:

```sql
SELECT set_config('bci.authenticated_subject', $1, true);
SELECT set_config('bci.authorized_roles', $2, true);
```

Both settings are transaction-local and come from the validated internal
security boundary. Data views can resolve opaque Metadata roles to client-owned
data scopes without storing user identities in the data database. Rendered
cache keys include SHA-256 hashes of the subject and normalized authorization
context, so a membership change or different role set cannot reuse another
scope's HTML.

## Interactive artifact data

Interactive artifacts can expose a database-owned query contract at:

```text
POST /artifacts/{client_key}/{artifact_key}/data
```

The JSON request body is opaque to Query Engine. For an artifact whose render
view is `{view_name}`, the client database defines `{view_name}_query(jsonb)`.
Query Engine authenticates the request, binds the trusted authorization
context, invokes that function with the unchanged JSON object, and returns its
JSON result unchanged.

The database function owns request validation and all client filtering,
sorting, pagination, calculation, and response-shape logic. Query Engine owns
only authorization, safe function resolution, request-size enforcement, and
freshness-aware Redis caching of the opaque request and response.

### Data-load cache prewarming

An artifact whose interactive response is identical for every authorized user
may explicitly opt into shared query caching by defining
`{view_name}_query_cache_scope(jsonb)` and returning `shared` for the exact
supported request. Contracts without that function, or requests for which it
returns `identity`, retain the subject-and-role-scoped cache key.

After a client data load commits its report-ready state, the client Data
Integration service may call the service-only endpoint:

```text
POST /internal/artifacts/{client_key}/{artifact_key}/query-cache/prewarm
```

with the fixed opaque requests to cache. The endpoint accepts only the stack's
service token, requires every request to be explicitly declared `shared` by
the database contract, executes the canonical query function, and writes the
same freshness-aware Redis keys used by authenticated browser requests. It
returns only cache status and entry counts; it never returns artifact rows.
The load must fail if the declared cache entries cannot be rebuilt, so a
successful load is also evidence that the next dashboard request is warm.

## Artifact static assets

Database-hosted web artifacts may publish immutable package-owned assets at:

```text
GET /artifacts/{client_key}/{artifact_key}/assets/{asset_path}
```

The authenticated route reads only active rows from `app.artifact_assets`,
returns the recorded media type, and emits a SHA-256 ETag plus a one-year
immutable private-cache policy. Asset URLs must therefore include a content
digest or version in their path. The client promotion package owns the asset
bytes and registry rows; Query Engine does not read client source directories
or expose a generic filesystem route.

## Running locally (with compose)

```bash
# from bci-container-orch
cd stacks/srp-local
docker compose up -d --build postgres credential-helper email-service query-engine
```

The SRP local stack exposes query-engine at `http://127.0.0.1:18300`.
The seeded `srp / visit-counts-quick-email` artifact sends to
`daniel@bicyclebi.com` and `jeanre@bicyclebi.com`.

SRP Dev runtime changes use the governed BCI Promote specification at
`deployment/promotions/dev-srp-query-engine-runtime.yml`. The Dev service
runtime lane stages the exact committed source revision, builds only
`query-engine`, recreates it with no dependencies, and validates Compose plus
service health.

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
## Usage Monitoring System

When the common `monitoring.events` and `monitoring.request_spans` tables are
installed in a client Metadata database, Query Engine records a metadata-only
request/response span for protected routes. The span includes the authenticated
user name/email, route template, response status, elapsed time, artifact key,
run ID, and available database/render/cache timings. It never stores request
bodies, query/filter values, rendered HTML, response bodies, recipient data, or
credentials.

Dashboards can emit explicit, allowlisted interaction events through:

```text
POST /usage/interactions/{client_key}/{artifact_key}
```

The interaction contract accepts only a type, stable interaction key, status,
and optional duration/reason code. Monitoring writes are best-effort and run
outside the response path so an observability outage cannot break a dashboard.
`log.artifact_runs` remains the authoritative dashboard execution lifecycle and
is correlated to request spans by `run_id`.
