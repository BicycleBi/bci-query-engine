-- BCI Analytics semantic and physical metadata layer.
--
-- This contract contains security metadata and bounded operational telemetry
-- only. It never exposes report payloads, client-data rows, recipients,
-- credentials, request bodies, or rendered output.

CREATE SCHEMA IF NOT EXISTS analytics_reporting;

CREATE TABLE IF NOT EXISTS analytics_reporting.client_contracts (
    client_key TEXT PRIMARY KEY,
    artifact_key TEXT NOT NULL DEFAULT 'usage-monitoring-dashboard',
    admin_role_key TEXT NOT NULL,
    client_reporting_role_key TEXT,
    allowed_delivery_modes TEXT[] NOT NULL DEFAULT ARRAY['web', 'email', 'both']::TEXT[],
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT analytics_reporting_client_key_chk
        CHECK (client_key ~ '^[a-z0-9][a-z0-9_-]{0,63}$'),
    CONSTRAINT analytics_reporting_artifact_key_chk
        CHECK (artifact_key ~ '^[a-z0-9][a-z0-9_-]{0,127}$'),
    CONSTRAINT analytics_reporting_delivery_modes_chk
        CHECK (allowed_delivery_modes <@ ARRAY['web', 'email', 'both']::TEXT[])
);

COMMENT ON TABLE analytics_reporting.client_contracts IS
    'Per-stack BCI Analytics semantic binding. Client packages own rows; Query Engine owns the shared views.';

CREATE OR REPLACE VIEW analytics_reporting.effective_assignments AS
WITH assignments AS (
    SELECT ur.client_key, ur.user_id, ur.role_key, 'direct'::TEXT AS source,
           NULL::TEXT AS group_key
    FROM security_user_roles ur
    WHERE ur.expires_at IS NULL OR ur.expires_at > NOW()
    UNION
    SELECT gm.client_key, gm.user_id, gr.role_key, 'group'::TEXT AS source,
           gm.group_key
    FROM security_group_members gm
    JOIN security_group_roles gr
      ON gr.client_key = gm.client_key AND gr.group_key = gm.group_key
    WHERE (gm.expires_at IS NULL OR gm.expires_at > NOW())
      AND (gr.expires_at IS NULL OR gr.expires_at > NOW())
)
SELECT a.client_key, a.user_id, u.display_name, u.email, u.active,
       a.role_key, a.source, a.group_key,
       a.role_key = c.admin_role_key AS is_bicycle_admin,
       c.client_reporting_role_key IS NOT NULL
           AND a.role_key = c.client_reporting_role_key AS is_client_reporter,
       c.admin_role_key, c.client_reporting_role_key, c.artifact_key
FROM assignments a
JOIN analytics_reporting.client_contracts c
  ON c.client_key = a.client_key AND c.enabled
JOIN security_users u ON u.user_id = a.user_id;

CREATE OR REPLACE VIEW analytics_reporting.effective_grants AS
SELECT a.client_key, a.user_id, a.display_name, a.email, a.active,
       a.role_key, a.source, a.group_key, a.is_bicycle_admin,
       a.is_client_reporter, a.admin_role_key, a.client_reporting_role_key,
       a.artifact_key, p.resource_key, p.permission_key
FROM analytics_reporting.effective_assignments a
LEFT JOIN security_role_permissions p ON p.role_key = a.role_key;

CREATE OR REPLACE VIEW analytics_reporting.client_grants AS
SELECT g.*
FROM analytics_reporting.effective_grants g
WHERE g.resource_key IS NULL
   OR g.resource_key = '*'
   OR split_part(g.resource_key, ':', 2) = g.client_key
   OR (split_part(g.resource_key, ':', 2) = '*'
       AND split_part(g.resource_key, ':', 3) = '*');

CREATE OR REPLACE VIEW analytics_reporting.active_user_audiences AS
WITH scoped_users AS (
    SELECT DISTINCT c.client_key, c.admin_role_key, c.client_reporting_role_key,
           u.user_id, u.display_name, u.email, u.active,
           COALESCE(bool_or(a.is_bicycle_admin), FALSE) AS is_bicycle_admin
    FROM analytics_reporting.client_contracts c
    JOIN security_users u
      ON u.client_key = c.client_key
      OR EXISTS (
          SELECT 1 FROM analytics_reporting.effective_assignments present
          WHERE present.client_key = c.client_key AND present.user_id = u.user_id
      )
    LEFT JOIN analytics_reporting.effective_assignments a
      ON a.client_key = c.client_key AND a.user_id = u.user_id
    WHERE c.enabled AND u.active
    GROUP BY c.client_key, c.admin_role_key, c.client_reporting_role_key,
             u.user_id, u.display_name, u.email, u.active
)
SELECT s.client_key, s.admin_role_key, s.client_reporting_role_key,
       s.user_id, s.display_name, s.email, s.active,
       audience.audience_key, s.is_bicycle_admin
FROM scoped_users s
CROSS JOIN LATERAL (
    VALUES ('all'::TEXT),
           (CASE WHEN s.is_bicycle_admin THEN 'bicycle' ELSE s.client_key END)
) audience(audience_key);

CREATE OR REPLACE VIEW analytics_reporting.reportable_artifacts AS
SELECT a.client_key, a.artifact_key, a.display_name, a.active,
       c.admin_role_key, c.client_reporting_role_key
FROM app.artifacts a
JOIN analytics_reporting.client_contracts c
  ON c.client_key = a.client_key AND c.enabled
WHERE a.delivery_mode = ANY(c.allowed_delivery_modes);

CREATE OR REPLACE VIEW analytics_reporting.artifact_access_edges AS
SELECT a.client_key, a.artifact_key, a.display_name AS artifact_name,
       a.active AS artifact_active, g.user_id, g.display_name, g.email,
       g.active, g.role_key, g.source, g.group_key, g.resource_key,
       g.permission_key, g.admin_role_key, g.client_reporting_role_key,
       g.is_bicycle_admin,
       g.permission_key IN ('artifact:read', '*') AS assigned_view,
       g.permission_key IN ('artifact:execute', '*') AS assigned_run
FROM analytics_reporting.reportable_artifacts a
JOIN analytics_reporting.effective_grants g
  ON g.client_key = a.client_key
 AND g.permission_key IN ('artifact:read', 'artifact:execute', '*')
 AND g.resource_key IN (
     'artifact:' || a.client_key || ':' || a.artifact_key,
     'artifact:' || a.client_key || ':*',
     'artifact:*:*',
     '*'
 );

CREATE OR REPLACE VIEW analytics_reporting.request_activity AS
SELECT DISTINCT u.client_key, u.user_id, s.request_id, s.started_at
FROM analytics_reporting.active_user_audiences u
JOIN monitoring.request_spans s
  ON s.client_key = u.client_key
 AND (s.user_id = u.user_id OR
      ((NULLIF(s.user_id, '') IS NULL OR NOT EXISTS (
          SELECT 1 FROM security_users known WHERE known.user_id = s.user_id
      )) AND lower(s.username) = lower(u.email)))
WHERE s.artifact_key IS NOT NULL
  AND s.route_template NOT IN (
      '/artifacts/{client_key}/{artifact_key}/usage-summary',
      '/artifacts/{client_key}/{artifact_key}/access-summary'
  );

CREATE OR REPLACE VIEW analytics_reporting.authenticated_denial_events AS
SELECT s.client_key, COALESCE(NULLIF(s.user_id, ''), lower(s.username)) AS subject_key,
       NULLIF(s.display_name, '') AS display_name,
       NULLIF(s.username, '') AS username,
       COALESCE(NULLIF(s.artifact_key, ''), 'unscoped-route') AS artifact_key,
       s.request_id, s.started_at
FROM monitoring.request_spans s
WHERE s.response_status = 403
  AND (NULLIF(s.user_id, '') IS NOT NULL OR NULLIF(s.username, '') IS NOT NULL);

COMMENT ON VIEW analytics_reporting.effective_assignments IS
    'Semantic layer for current, unexpired direct and group role assignments.';
COMMENT ON VIEW analytics_reporting.active_user_audiences IS
    'Semantic audience membership for active scoped users: all plus Bicycle or client.';
COMMENT ON VIEW analytics_reporting.artifact_access_edges IS
    'Effective artifact access relationships after exact and wildcard grant resolution.';
COMMENT ON VIEW analytics_reporting.request_activity IS
    'Metadata-only request identities and timestamps for bounded period aggregation.';
COMMENT ON VIEW analytics_reporting.authenticated_denial_events IS
    'Authenticated HTTP 403 metadata for bounded aggregation; contains no request payloads.';
