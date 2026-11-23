from pathlib import Path
from pglast import parse_sql

SCHEMA = (Path(__file__).resolve().parents[1] / "docs/analytics-reporting-schema.sql").read_text()


def test_database_schema_owns_identity_expiry_audience_and_wildcard_semantics():
    parse_sql(SCHEMA)
    assert "CREATE TABLE IF NOT EXISTS analytics_reporting.client_contracts" in SCHEMA
    assert "CREATE OR REPLACE VIEW analytics_reporting.effective_assignments" in SCHEMA
    assert "CREATE OR REPLACE VIEW analytics_reporting.active_user_audiences" in SCHEMA
    assert "CREATE OR REPLACE VIEW analytics_reporting.authorized_user_audiences" in SCHEMA
    assert "CREATE OR REPLACE VIEW analytics_reporting.artifact_access_edges" in SCHEMA
    assert "ur.expires_at IS NULL OR ur.expires_at > NOW()" in SCHEMA
    assert "gm.expires_at IS NULL OR gm.expires_at > NOW()" in SCHEMA
    assert "gr.expires_at IS NULL OR gr.expires_at > NOW()" in SCHEMA
    assert "a.role_key = c.admin_role_key AS is_bicycle_admin" in SCHEMA
    assert "'artifact:' || a.client_key || ':*'" in SCHEMA
    assert "'artifact:*:*'" in SCHEMA


def test_database_schema_is_client_data_free_and_has_no_client_literal():
    lowered = SCHEMA.lower()
    assert "'srp'" not in lowered
    assert "'rf'" not in lowered
    assert "from src." not in lowered
    assert "from ods." not in lowered
    assert "from trn." not in lowered
    assert "from pds." not in lowered
    assert "payload_json" not in lowered
