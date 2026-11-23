# Artifact contract

Version artifact source in the client repository. Deliver the active registration, templates and assets into Metadata and report views/functions into Data through the framework. The shared image requires no client source package.

For a direct display the engine renders the declared view with Jinja variables `rows` and `columns`. For an interactive request it passes bounded JSON to the view's companion `<view>_query(jsonb)` function. The Data transaction is read-only and carries the verified subject and effective roles. Shared query caches require a database-declared shared scope. Personal cache entries include identity and authorization scope. An optional `<view>_query_timeout_seconds()` function can set a transaction-local query budget of 1–120 seconds for Data queries and prewarming. The default is 30 seconds.

Use the [display](examples/onboarding/display-execution.json) or [dry-run](examples/onboarding/dry-run-execution.json) request with `POST /artifact-executions`. Both are synchronous. Display returns `preview_html`; dry-run records the outcome. A composed email can also return `email_subject`. One existing body reference is supported for simple wrappers. For multiple sections use ordered, named Metadata slots with explicit parameter mappings. The wrapper and referenced content must belong to the same client. A content query is responsible for returning the caller's authorized slice.

A named Metadata report query uses `POST /artifacts/{client}/{artifact}/metadata/{query_key}`. The stack registers its trusted schema-qualified function and access policy. The route supplies JSON parameters and a read-only Metadata transaction. Existing platform usage/access policies enforce their additional reporting grants. Use a Data presentation query for business results.

PDFs use declared slice labels; CSV requires a JSON result with a `csv` object containing `columns` (key and label) and `rows`. File export requires explicit output storage. Distribution destinations, approvals and workflow actions belong with their owners.
