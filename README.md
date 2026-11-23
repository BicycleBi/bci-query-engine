# Bicycle Query Engine

Query Engine serves authenticated artifacts. It reads the active artifact and template from Metadata, executes its declared Data view or parameterized function, and returns HTML or JSON. Redis stores derived results. Ordinary report requests go directly from the authorized ingress to Query Engine.

## HTTP API

| Method and route | Purpose |
| --- | --- |
| GET /health | Service health |
| GET /artifacts/{client}/{artifact} | Render HTML under the caller's identity |
| POST /artifacts/{client}/{artifact}/data | Bind the request JSON to the declared Data query |
| POST /artifacts/{client}/{artifact}/metadata/{query_key} | Execute a registered read-only Metadata query |
| GET /artifacts/{client}/{artifact}/assets/{path} | Return the authorized asset and its content ETag |
| POST /artifact-executions | Synchronously render a display or dry-run request, including explicitly requested file outputs |
| GET /artifact-executions/{run_id} | Read the authorized run status |
| POST /internal/artifacts/{client}/{artifact}/query-cache/prewarm | Prepare explicitly shared Redis results using the existing service credential |
| POST /usage/interactions/{client}/{artifact}, POST /internal/usage/events | Record bounded telemetry |

The signed stack identity identifies the subject and client. Query Engine reads the stack's existing Security grants and current direct/group roles before serving the artifact, including a cache hit. Display/assets require artifact:read. JSON queries and render executions require artifact:execute. The stack supplies its existing database and signing credentials. The framework continues to use app.db.get_data_conn, app.db.get_metadata_conn and app.cache for stack operations.

## Data and rendering

A client repository owns its semantic definitions, presentation queries, templates and access declarations. Publish those changes through the development framework. The [artifact contract](docs/artifact-contract-and-examples.md) describes query and render inputs. Named Metadata queries use the [registration schema](docs/artifact-metadata-queries.sql). The database function owns report semantics. Ordered email composition uses the optional [layout and slots schema](docs/artifact-email-composition.sql). The Coordinator can pass parameters to an authenticated display execution and hand the resulting HTML and subject to Email Service.

HTML and JSON rendering require no output volume. For retained PDF or CSV outputs, provision the [output log](sql/006_artifact_outputs.sql) through schema delivery and mount persistent writable storage at ARTIFACT_OUTPUT_DIR. The renderer writes the file and records its location. Use slice_key and slice_label for per-row PDFs. Apply schema changes separately from container recreation. Redis prewarming preserves the database-declared scope and cache policy, including generation swaps. Telemetry remains bounded to the existing [monitoring schema](docs/usage-monitoring-schema.sql).

## Testing the development branch

Build this repository's committed development branch through the framework using [the container recipe](framework/container.json). Verify a permitted report display, named query, asset and cache refresh, then repeat with a denied user, including against a warm cache. Where enabled, test a Coordinator render and verify the intended scope before triggering a separate distribution.

Before switching an existing stack, inventory its actual callers. Migrate definition writes to framework content delivery, manual intake to the authenticated Coordinator action, sends/retries to Coordinator and Email Service, support requests to their owning application, and dashboard-specific summary APIs to database-backed artifact queries. Check effective grants for formerly name-restricted artifacts, including users with wildcard grants. Stack owners must carry those narrower entitlements and permitted reporting audiences into their declarations before cutover. Client-specific role variable fallbacks are retired. The engine accepts the shared ANALYTICS_REPORTING_CLIENT_KEY, ANALYTICS_BICYCLE_ADMIN_ROLE and ANALYTICS_CLIENT_REPORTING_ROLE settings for existing platform analytics contracts.

Run the source suite with python -m pytest. The optional disposable PostgreSQL integration test is described in its module.
