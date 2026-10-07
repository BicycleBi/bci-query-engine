# SRP QuickBooks load progress contract

The SRP Profit and Loss report queues its manual QuickBooks refresh through the
durable Query Engine data-load action. Query Engine remains the authorization
and persistence boundary; the browser never calls Data Integration directly.

For a running action, Query Engine requests bounded progress from the internal
Data Integration endpoint using the shared service credential. The response is
normalized before it can reach the browser. Allowed fields are the current
company, current stage, company counts, progress timestamp, and allowlisted
failure categories.

`log.data_load_actions.progress` stores the final safe progress summary. This
supports the terminal state `completed_with_warnings`, which means at least one
company refreshed and at least one company retained its previous data after a
failed refresh.

The browser-visible failure messages are fixed by Query Engine. Data
Integration exception strings, provider responses, credentials, Realm IDs,
financial rows, and QuickBooks response bodies are not included.

The internal default progress route is:

```text
http://data-integration:8080/loads/srp-quickbooks-loader/progress/{action_id}
```

It can be overridden with `QUICKBOOKS_FULL_LOAD_PROGRESS_URL`, but the value
must retain the `{action_id}` placeholder. Failure to obtain live progress does
not fail the load or expose an internal error; the action remains `running`
until the worker receives the terminal loader response.

