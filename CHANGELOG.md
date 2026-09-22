# Changelog

## Unreleased

- Move BCI Analytics assignment, audience, artifact, wildcard-grant, activity,
  and authenticated-denial semantics into an explicit metadata-database layer.
- Allow bounded audience filtering on the compact Access summary while
  preserving the existing user/artifact matrix API.

## 0.4.0 - 2026-09-18

### Added

- Authenticated, client-scoped Usage Monitoring and Access reporting.
- Client-aware, fail-closed Analytics authorization for SRP, RF, RAG, and RBP.
- Per-stack Analytics role configuration and trusted data-transaction context.
- Semantic reporting and physical metadata/data database documentation.
- Active-user-only access results and web-artifact-only SRP catalog scope.

### Compatibility

- No HTTP operations were removed.
- Existing request and response schemas were not removed or changed.
- Existing routes gained only optional query/header parameters.
- Legacy `/run` compatibility routes remain available.
- SRP retains its existing role-variable compatibility path.
- RF, RAG, and RBP adopt the generic Analytics variables only when this
  version replaces Query Engine in that individual stack.

### Per-stack replacement contract

When this version is introduced to an RF, RAG, or RBP stack, the same governed
replacement must set:

- `ANALYTICS_REPORTING_CLIENT_KEY`
- `ANALYTICS_BICYCLE_ADMIN_ROLE`
- `ANALYTICS_CLIENT_REPORTING_ROLE` when that stack has an approved
  client-reporting audience

Stacks that are not replaced remain on their existing Query Engine runtime and
are unaffected by this tag. Production is outside the current rollout scope.

### Development

- Declare `pglast`, which is required by the SQL parser test suite, in the
  development dependency set.
