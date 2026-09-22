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
    )
    SELECT max(d.display_name), max(d.username), d.artifact_key,
           count(DISTINCT d.request_id), max(d.started_at)
    FROM included d
    -- Keep known and unmapped subject namespaces distinct; do not merge an
    -- ambiguous email or another client's identity into a registered user.
    GROUP BY (d.resolved_user_id IS NOT NULL),
             COALESCE(d.resolved_user_id, d.subject_key), d.artifact_key
    ORDER BY max(d.started_at) DESC, d.artifact_key,
             (d.resolved_user_id IS NOT NULL),
             COALESCE(d.resolved_user_id, d.subject_key)
    LIMIT LEAST(GREATEST(p_limit, 1), 201);
$summary$;

COMMENT ON FUNCTION analytics_reporting.authenticated_denial_summary(
    TEXT, TEXT, TEXT, TIMESTAMPTZ, TIMESTAMPTZ, INTEGER
) IS 'Bounded authenticated HTTP 403 aggregates, independent of account paging. All includes unmapped and inactive subjects; narrower audiences require current database membership.';
