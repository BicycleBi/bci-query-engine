-- BCI Usage Monitoring System schema contract.
-- Query Engine is the sole database writer. The tables contain named identity
-- and bounded timing metadata, never credentials, tokens, bodies, filters,
-- rendered output, recipients, or client-data values.

CREATE SCHEMA IF NOT EXISTS monitoring;

CREATE TABLE IF NOT EXISTS monitoring.events (
    event_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    service_name TEXT NOT NULL,
    event_type TEXT NOT NULL,
    event_key TEXT,
    event_status TEXT NOT NULL,
    client_key TEXT,
    user_id TEXT,
    username TEXT,
    email TEXT,
    display_name TEXT,
    session_id UUID,
    request_id TEXT,
    artifact_key TEXT,
    run_id UUID,
    reason_code TEXT,
    duration_ms INTEGER,
    http_status SMALLINT,
    CONSTRAINT monitoring_events_service_chk CHECK (service_name ~ '^[a-z0-9][a-z0-9-]{0,63}$'),
    CONSTRAINT monitoring_events_type_chk CHECK (event_type ~ '^[a-z][a-z0-9_]{0,79}$'),
    CONSTRAINT monitoring_events_key_chk CHECK (event_key IS NULL OR event_key ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'),
    CONSTRAINT monitoring_events_status_chk CHECK (event_status IN ('started', 'completed', 'succeeded', 'failed', 'denied')),
    CONSTRAINT monitoring_events_reason_chk CHECK (reason_code IS NULL OR reason_code ~ '^[a-z][a-z0-9_]{0,79}$'),
    CONSTRAINT monitoring_events_duration_chk CHECK (duration_ms IS NULL OR duration_ms BETWEEN 0 AND 86400000),
    CONSTRAINT monitoring_events_http_status_chk CHECK (http_status IS NULL OR http_status BETWEEN 100 AND 599)
);

CREATE TABLE IF NOT EXISTS monitoring.request_spans (
    request_id TEXT PRIMARY KEY,
    service_name TEXT NOT NULL,
    client_key TEXT,
    user_id TEXT,
    username TEXT,
    email TEXT,
    display_name TEXT,
    session_id UUID,
    method TEXT NOT NULL,
    route_template TEXT NOT NULL,
    artifact_key TEXT,
    run_id UUID,
    started_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ NOT NULL,
    duration_ms INTEGER NOT NULL,
    response_status SMALLINT NOT NULL,
    outcome TEXT NOT NULL,
    reason_code TEXT,
    gateway_duration_ms NUMERIC(14,3),
    upstream_connect_ms NUMERIC(14,3),
    upstream_header_ms NUMERIC(14,3),
    upstream_response_ms NUMERIC(14,3),
    database_ms NUMERIC(14,3),
    render_ms NUMERIC(14,3),
    cache_status TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT monitoring_request_spans_service_chk CHECK (service_name ~ '^[a-z0-9][a-z0-9-]{0,63}$'),
    CONSTRAINT monitoring_request_spans_method_chk CHECK (method ~ '^[A-Z]{3,12}$'),
    CONSTRAINT monitoring_request_spans_duration_chk CHECK (duration_ms BETWEEN 0 AND 86400000),
    CONSTRAINT monitoring_request_spans_response_status_chk CHECK (response_status BETWEEN 100 AND 599),
    CONSTRAINT monitoring_request_spans_outcome_chk CHECK (outcome IN ('completed', 'failed', 'denied')),
    CONSTRAINT monitoring_request_spans_reason_chk CHECK (reason_code IS NULL OR reason_code ~ '^[a-z][a-z0-9_]{0,79}$'),
    CONSTRAINT monitoring_request_spans_timings_chk CHECK (
        (gateway_duration_ms IS NULL OR gateway_duration_ms >= 0) AND
        (upstream_connect_ms IS NULL OR upstream_connect_ms >= 0) AND
        (upstream_header_ms IS NULL OR upstream_header_ms >= 0) AND
        (upstream_response_ms IS NULL OR upstream_response_ms >= 0) AND
        (database_ms IS NULL OR database_ms >= 0) AND
        (render_ms IS NULL OR render_ms >= 0)
    )
);

-- Additive upgrade path for environments that exercised the earlier
-- development contract before gateway correlation was introduced.
ALTER TABLE monitoring.request_spans ADD COLUMN IF NOT EXISTS gateway_duration_ms NUMERIC(14,3);
ALTER TABLE monitoring.request_spans ADD COLUMN IF NOT EXISTS upstream_connect_ms NUMERIC(14,3);
ALTER TABLE monitoring.request_spans ADD COLUMN IF NOT EXISTS upstream_header_ms NUMERIC(14,3);

CREATE INDEX IF NOT EXISTS monitoring_events_client_period_idx ON monitoring.events (client_key, occurred_at DESC);
CREATE INDEX IF NOT EXISTS monitoring_events_user_period_idx ON monitoring.events (client_key, username, occurred_at DESC) WHERE username IS NOT NULL;
CREATE INDEX IF NOT EXISTS monitoring_events_artifact_period_idx ON monitoring.events (client_key, artifact_key, occurred_at DESC) WHERE artifact_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS monitoring_request_spans_client_period_idx ON monitoring.request_spans (client_key, started_at DESC);
CREATE INDEX IF NOT EXISTS monitoring_request_spans_route_period_idx ON monitoring.request_spans (client_key, route_template, started_at DESC);

COMMENT ON SCHEMA monitoring IS 'Named-user application usage and service-timing telemetry; excludes secrets and business payloads.';
COMMENT ON TABLE monitoring.events IS 'Login, authorization, dashboard-run, and UI interaction lifecycle events.';
COMMENT ON TABLE monitoring.request_spans IS 'One bounded metadata-only record per correlated service request/response lifecycle.';
