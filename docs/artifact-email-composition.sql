-- Optional, stack-owned Metadata schema for generic email composition.
CREATE TABLE IF NOT EXISTS app.artifact_email_composition (
    artifact_id uuid PRIMARY KEY REFERENCES app.artifacts(artifact_id) ON DELETE CASCADE,
    subject_template text NOT NULL CHECK (length(subject_template) BETWEEN 1 AND 998)
);

CREATE TABLE IF NOT EXISTS app.artifact_email_content (
    artifact_id uuid NOT NULL REFERENCES app.artifact_email_composition(artifact_id) ON DELETE CASCADE,
    slot_key text NOT NULL CHECK (slot_key ~ '^[a-z][a-z0-9_]{0,62}$'),
    position integer NOT NULL CHECK (position >= 0),
    content_artifact_id uuid NOT NULL REFERENCES app.artifacts(artifact_id),
    parameter_map jsonb CHECK (jsonb_typeof(parameter_map) = 'object'),
    PRIMARY KEY (artifact_id, slot_key),
    UNIQUE (artifact_id, position),
    CHECK (artifact_id <> content_artifact_id)
);
REVOKE ALL ON app.artifact_email_composition, app.artifact_email_content FROM PUBLIC;
