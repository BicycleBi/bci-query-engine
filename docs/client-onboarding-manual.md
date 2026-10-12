# Query Engine Artifact Contract And Examples

Status: Active service reference
Scope: client-agnostic Query Engine data, artifact, rendering, and execution contracts

For the full client onboarding sequence, start with the
[orchestration onboarding guide](https://github.com/BicycleBi/bci-container-orch/blob/main/docs/deployment/client-onboarding-guide.md).
The owning client repository defines its data, templates, audience, and delivery
configuration. This document explains the Query Engine part of that work.

The `example` client and `example.invalid` address below are synthetic and
cannot be used as a live client package.

## Data And Rendering Contract

The client repository owns the schema-qualified relation named by `view_name`.
Query Engine reads that relation through its configured Data connection and
passes row dictionaries as `rows` to Jinja2. `columns` contains the first
row's column names. The relation, its grants, and freshness validation are
client-owned; artifact upsert does not create them.

The [synthetic rows](examples/onboarding/weekly-summary-rows.json) illustrate
one row per team and report week, with `report_week`, `team_name`, and
`completed_items`. A possible fixture query is:

```sql
SELECT DATE '2026-01-05' AS report_week,
       'Example North'::text AS team_name,
       25::integer AS completed_items
UNION ALL
SELECT DATE '2026-01-05', 'Example South'::text, 18::integer;
```

This is synthetic example data, not a live validation query. Render the fixture
locally without copying a client database or client rows. Interactive artifacts
may additionally define `{view_name}_query(jsonb)` for filtering and paging;
see [Interactive artifact data](../README.md#interactive-artifact-data).

## Artifact Definitions

The [web artifact request](examples/onboarding/web-artifact.json) has a direct
source: both `view_name` and `template` are supplied. The template loops over
`rows` and includes a zero-row state; Query Engine HTML-escapes values.

The [email artifact request](examples/onboarding/email-artifact.json) is a
separate wrapper with one `body` reference to the web artifact. It has no
direct view or template. Its recipient uses a non-deliverable domain.

These files show the `POST /artifacts` request body shape. That endpoint
upserts client, template, and artifact metadata, and **replaces** the complete
static recipient and reference lists. Omitted lists default to empty. Review
the full intended definition before an update. Client data migrations,
authorization, navigation, and schedules use their own owning workflows.

Protected routes require a signed internal bearer token for the same client.
Display and execution also require the authenticated subject and artifact
permission. Use the installed Security and ingress route for real checks.

## Display And Execution Examples

| Example | Request | Effect |
| --- | --- | --- |
| Web display | `GET /artifacts/example/weekly-summary` | Returns authenticated rendered HTML |
| [Display execution](examples/onboarding/display-execution.json) | `POST /artifact-executions` | Renders preview and records a run; no email |
| [Email dry-run](examples/onboarding/dry-run-execution.json) | `POST /artifact-executions` | Renders and records a run; no email |

Always set `behavior` explicitly. The API default is `deliver`, so omitting
the field can send mail. `display` and `dry-run` still render data and write
execution evidence. PDF is an optional output. CSV requires the
database-owned query contract and an execution `query`.

An authorized delivery can return a queued run. Check
`GET /artifact-executions/{run_id}` before deciding whether to retry an
uncertain send. `provider_accepted` records provider acceptance; it does not
prove trace-confirmed delivery. The [HTTP API](../README.md#http-api) describes
the current routes and status semantics.

The shared examples are safe for model and local synthetic rendering checks.
For client-backed checks, follow the orchestration guide and its data-safety
policy. Keep raw client rows, recipient lists, rendered client reports, and
secrets out of AI-visible evidence.
