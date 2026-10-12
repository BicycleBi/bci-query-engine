# RAG QA Security Access wildcard authorization correction

RAG QA's existing administrator role grants `artifact:read` on `artifact:rag:*`.
The Security Access API reader requires an exact report resource, so it rejects
that existing permission even when Security permits the page. The signed-in QA
page was rechecked on 12 October and displays a permission denial. This is a
confirmed source mismatch; individual account membership and the running API
revision have not been independently diagnosed by this source test.

The correction follows Security's existing canonical resource hierarchy:
exact artifact, same-client artifact wildcard, artifact-wide wildcard, global
wildcard; permissions match the report-specific permission or `*`. It retains
active-user, client, identity/email, configured admin/client role and contract
checks. Artifact read alone cannot authorize Usage, which requires `usage:read`
or an existing wildcard permission. No grants or memberships change.

The new synthetic test executes the authorization SQL predicates in an in-memory
SQLite catalog with only PostgreSQL bind/array syntax adapted. Against the old
source, 9 tests fail and 13 pass; the corrected source passes all 22. Combined
with existing Usage Access, configuration and denial-contract regressions,
90 tests pass. No real database or client rows were used. The configured-client
role remains governed by existing tests; this is not a new client audience grant.

Source base: `0a6eab59e812f43f5606fcbab7f0868894d733dc`.
Test command:
`python -m pytest -q tests/test_reporting_wildcard_authorization.py tests/test_usage_access.py tests/test_analytics_config.py tests/test_analytics_denial_contract.py`

This source PR does not deploy a runtime. Review/merge under the protected
Query Engine source workflow is separate from RAG Dev qualification and QA
promotion. Prepare a build-once, exact-source runtime package with the actual
QA predecessor and qualified recovery image; preserve the QA environment,
report templates, data contracts, Home, grants and every unrelated client.
Do not promote the current main's unrelated changes simply to obtain this fix.
If necessary, reconcile only this unchanged accepted fix onto the exact existing
runtime source through the governing release workflow. Require fresh package
admission/checks/native execution approval and browser data acceptance.
The completed 9 October three-member correction must not be replayed.
Production and all running environments remain unchanged by this source work.
