-- Install in the stack's Metadata database under its schema owner.
CREATE TABLE IF NOT EXISTS app.artifact_metadata_queries (
    artifact_id uuid NOT NULL REFERENCES app.artifacts (artifact_id) ON DELETE CASCADE,
    query_key text NOT NULL CHECK (query_key ~ '^[a-z][a-z0-9_-]{0,62}$'),
    function_name text NOT NULL CHECK (function_name ~ '^[a-z_][a-z0-9_]{0,62}[.][a-z_][a-z0-9_]{0,62}$'),
    access_policy text NOT NULL CHECK (access_policy IN ('artifact', 'usage', 'access')),
    PRIMARY KEY (artifact_id, query_key)
);
REVOKE ALL ON app.artifact_metadata_queries FROM PUBLIC;
