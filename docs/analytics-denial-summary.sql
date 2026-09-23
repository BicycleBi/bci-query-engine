-- Additive shared metadata contract. Install after analytics-reporting-schema.sql.
-- No grants are changed. The caller must pass the live report authorization gate.
-- This function is invoker-rights and never accepts SQL or a caller-selected relation.
CREATE OR REPLACE FUNCTION analytics_reporting.authenticated_denial_summary(
    p_client TEXT, p_admin_role TEXT, p_audience TEXT,
    p_start TIMESTAMPTZ, p_end TIMESTAMPTZ, p_limit INTEGER
) RETURNS TABLE (
    display_name TEXT, username TEXT, artifact_key TEXT,
    denied_requests BIGINT, last_denied_at TIMESTAMPTZ
)
LANGUAGE SQL STABLE SECURITY INVOKER
SET search_path = pg_catalog, public
AS $summary$
    WITH scoped AS (
        SELECT d.*, resolved.user_id AS resolved_user_id
        FROM analytics_reporting.authenticated_denial_events d
        JOIN analytics_reporting.client_contracts c
          ON c.client_key = d.client_key AND c.enabled
         AND c.admin_role_key = p_admin_role
        LEFT JOIN LATERAL (
            -- Exact known subjects win. Email fallback is permitted only for an
            -- unknown provider subject and exactly one client-scoped account.
            SELECT min(u.user_id) AS user_id
            FROM public.security_users u
            WHERE (u.client_key = d.client_key OR EXISTS (
                SELECT 1 FROM analytics_reporting.effective_assignments a
                WHERE a.client_key = d.client_key AND a.user_id = u.user_id
            ))
              AND (u.user_id = d.subject_key OR (
                  NOT EXISTS (SELECT 1 FROM public.security_users known
                              WHERE known.user_id = d.subject_key)
                  AND lower(u.email) = lower(d.username)
              ))
            HAVING count(*) = 1
        ) resolved ON TRUE
        WHERE d.client_key = p_client
          AND p_audience IN ('all', 'bicycle', p_client)
          AND p_start < p_end AND p_end - p_start <= INTERVAL '90 days'
          AND p_limit BETWEEN 1 AND 201
          AND d.started_at >= p_start AND d.started_at < p_end
    ), included AS (
        SELECT s.* FROM scoped s
        WHERE p_audience = 'all' OR EXISTS (
            SELECT 1 FROM analytics_reporting.active_user_audiences a
            WHERE a.client_key = s.client_key
              AND a.user_id = s.resolved_user_id
              AND a.admin_role_key = p_admin_role
              AND a.audience_key = p_audience
        )
    ), request_denials AS (
        SELECT max(d.display_name) AS display_name,
               max(d.username) AS username,
               d.artifact_key,
               count(DISTINCT d.request_id) AS denied_requests,
               max(d.started_at) AS last_denied_at
        FROM included d
        -- Keep known and unmapped subject namespaces distinct; do not merge an
        -- ambiguous email or another client's identity into a registered user.
        GROUP BY (d.resolved_user_id IS NOT NULL),
                 COALESCE(d.resolved_user_id, d.subject_key), d.artifact_key
    ), authenticated_without_grants AS (
        SELECT max(u.display_name) AS display_name,
               max(u.email) AS username,
               'No current artifact grants'::TEXT AS artifact_key,
               count(DISTINCT audit.audit_id) AS denied_requests,
               max(audit.created_at) AS last_denied_at
        FROM public.security_audit_log audit
        JOIN public.security_users u
          ON u.user_id = audit.user_id
         AND u.client_key = audit.client_key
         AND u.active
        JOIN analytics_reporting.client_contracts c
          ON c.client_key = audit.client_key
         AND c.enabled
         AND c.admin_role_key = p_admin_role
        WHERE audit.client_key = p_client
          AND audit.event_type = 'identity_upsert'
          AND audit.event_status = 'ok'
          AND p_audience IN ('all', 'bicycle', p_client)
          AND p_start < p_end AND p_end - p_start <= INTERVAL '90 days'
          AND p_limit BETWEEN 1 AND 201
          AND audit.created_at >= p_start AND audit.created_at < p_end
          AND NOT EXISTS (
              SELECT 1
              FROM analytics_reporting.artifact_access_edges edge
              WHERE edge.client_key = audit.client_key
                AND edge.user_id = audit.user_id
                AND edge.admin_role_key = p_admin_role
                AND edge.active
                AND edge.artifact_active
                AND edge.permission_key IN ('artifact:read', 'artifact:execute', '*')
          )
          AND (p_audience = 'all' OR EXISTS (
              SELECT 1
              FROM analytics_reporting.active_user_audiences audience
              WHERE audience.client_key = audit.client_key
                AND audience.user_id = audit.user_id
                AND audience.admin_role_key = p_admin_role
                AND audience.audience_key = p_audience
          ))
        GROUP BY audit.user_id
    ), combined AS (
        SELECT * FROM request_denials
        UNION ALL
        SELECT * FROM authenticated_without_grants
    )
    SELECT display_name, username, artifact_key,
           denied_requests, last_denied_at
    FROM combined
    ORDER BY last_denied_at DESC, artifact_key, username
    LIMIT LEAST(GREATEST(p_limit, 1), 201);
$summary$;

COMMENT ON FUNCTION analytics_reporting.authenticated_denial_summary(
    TEXT, TEXT, TEXT, TIMESTAMPTZ, TIMESTAMPTZ, INTEGER
) IS 'Bounded authenticated HTTP 403 aggregates plus successful authentications with no current artifact grants, independent of account paging.';
