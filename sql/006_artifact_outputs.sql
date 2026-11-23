CREATE TABLE IF NOT EXISTS log.artifact_outputs (
    output_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID NOT NULL,
    artifact_id UUID,
    artifact_key TEXT NOT NULL,
    client_key TEXT NOT NULL,
    output_format TEXT NOT NULL,
    output_role TEXT,
    slice_key TEXT,
    slice_label TEXT,
    filename TEXT NOT NULL,
    storage_path TEXT NOT NULL,
    content_type TEXT NOT NULL,
    file_size_bytes BIGINT NOT NULL,
    sha256 TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'completed',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS artifact_outputs_run_idx
ON log.artifact_outputs (run_id);

CREATE INDEX IF NOT EXISTS artifact_outputs_artifact_idx
ON log.artifact_outputs (client_key, artifact_key, created_at DESC);
