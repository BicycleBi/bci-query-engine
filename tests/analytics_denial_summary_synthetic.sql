-- Secured-host synthetic PostgreSQL rehearsal. NOT for any client database.
-- AI-safe output: fixed pass/fail only. No real identities or telemetry inputs.
-- Run with psql -X -v ON_ERROR_STOP=1 -f against a fresh disposable database
-- named analytics_synthetic_<suffix>, under separately governed host authority.
\set ON_ERROR_STOP on
BEGIN;
DO $guard$
BEGIN
    IF current_database() NOT LIKE 'analytics\_synthetic\_%' ESCAPE '\'
       OR to_regclass('public.security_users') IS NOT NULL
       OR to_regnamespace('analytics_reporting') IS NOT NULL
       OR to_regnamespace('monitoring') IS NOT NULL THEN
        RAISE EXCEPTION 'synthetic_database_required';
    END IF;
END
$guard$;
CREATE TABLE public.security_users (user_id TEXT PRIMARY KEY, client_key TEXT, email TEXT);
CREATE SCHEMA analytics_reporting;
CREATE TABLE analytics_reporting.client_contracts (client_key TEXT PRIMARY KEY, admin_role_key TEXT, enabled BOOLEAN);
CREATE TABLE analytics_reporting.effective_assignments (client_key TEXT, user_id TEXT);
CREATE TABLE analytics_reporting.active_user_audiences (client_key TEXT, user_id TEXT, admin_role_key TEXT, audience_key TEXT);
CREATE TABLE analytics_reporting.authenticated_denial_events (
    client_key TEXT, subject_key TEXT, display_name TEXT, username TEXT,
    artifact_key TEXT, request_id TEXT, started_at TIMESTAMPTZ
);
\ir ../docs/analytics-denial-summary.sql
INSERT INTO public.security_users VALUES
    ('admin', 'alpha', 'admin@example.test'),
    ('reader', 'alpha', 'reader@example.test'),
    ('disabled', 'alpha', 'disabled@example.test'),
    ('other', 'beta', 'reader@example.test'),
    ('duplicate-a', 'alpha', 'duplicate@example.test'),
    ('duplicate-b', 'alpha', 'duplicate@example.test');
INSERT INTO analytics_reporting.client_contracts VALUES ('alpha', 'alpha_admin', TRUE), ('beta', 'beta_admin', TRUE);
INSERT INTO analytics_reporting.active_user_audiences VALUES
    ('alpha', 'admin', 'alpha_admin', 'bicycle'),
    ('alpha', 'reader', 'alpha_admin', 'alpha');
INSERT INTO analytics_reporting.authenticated_denial_events VALUES
    ('alpha','admin','Synthetic admin','admin@example.test','home','r1','2026-09-22T10:00:00Z'),
    ('alpha','provider-admin','Synthetic admin','ADMIN@example.test','home','r2','2026-09-22T10:00:00Z'),
    ('alpha','reader','Synthetic reader','reader@example.test','home','r3','2026-09-22T10:00:00Z'),
    ('alpha','unmapped','Synthetic unmapped','unknown@example.test','home','r4','2026-09-22T10:00:00Z'),
    ('alpha','disabled','Synthetic inactive','disabled@example.test','home','r5','2026-09-22T10:00:00Z'),
    ('alpha','other','Synthetic foreign','reader@example.test','home','r6','2026-09-22T10:00:00Z'),
    ('alpha','ambiguous','Synthetic ambiguous','duplicate@example.test','home','r7','2026-09-22T10:00:00Z'),
    ('beta','other','Synthetic beta','reader@example.test','home','r8','2026-09-22T10:00:00Z'),
    ('alpha','admin','Synthetic old','admin@example.test','home','r9','2026-01-01T10:00:00Z');
DO $checks$
DECLARE n BIGINT; events BIGINT;
BEGIN
    SELECT count(*), sum(denied_requests) INTO n, events
    FROM analytics_reporting.authenticated_denial_summary('alpha','alpha_admin','all','2026-09-01','2026-09-23',201);
    IF n <> 6 OR events <> 7 THEN RAISE EXCEPTION 'all_subjects_contract_failed'; END IF;
    SELECT count(*), sum(denied_requests) INTO n, events
    FROM analytics_reporting.authenticated_denial_summary('alpha','alpha_admin','bicycle','2026-09-01','2026-09-23',201);
    IF n <> 1 OR events <> 2 THEN RAISE EXCEPTION 'provider_identity_resolution_failed'; END IF;
    SELECT count(*), sum(denied_requests) INTO n, events
    FROM analytics_reporting.authenticated_denial_summary('alpha','alpha_admin','alpha','2026-09-01','2026-09-23',201);
    IF n <> 1 OR events <> 1 THEN RAISE EXCEPTION 'client_audience_isolation_failed'; END IF;
    SELECT count(*) INTO n
    FROM analytics_reporting.authenticated_denial_summary('alpha','beta_admin','all','2026-09-01','2026-09-23',201);
    IF n <> 0 THEN RAISE EXCEPTION 'runtime_binding_mismatch_failed'; END IF;
    SELECT count(*) INTO n
    FROM analytics_reporting.authenticated_denial_summary('alpha','alpha_admin','unknown','2026-09-01','2026-09-23',201);
    IF n <> 0 THEN RAISE EXCEPTION 'unknown_audience_failed'; END IF;
    SELECT count(*) INTO n
    FROM analytics_reporting.authenticated_denial_summary('alpha','alpha_admin','all','2026-01-01','2026-09-23',201);
    IF n <> 0 THEN RAISE EXCEPTION 'period_bound_failed'; END IF;
    SELECT count(*) INTO n
    FROM analytics_reporting.authenticated_denial_summary('alpha','alpha_admin','all','2026-09-01','2026-09-23',1);
    IF n <> 1 THEN RAISE EXCEPTION 'row_bound_failed'; END IF;
END
$checks$;
ROLLBACK;
SELECT 'analytics_denial_summary_synthetic_pass' AS validation;
