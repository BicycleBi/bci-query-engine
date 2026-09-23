"""Verify Query Engine consumes the database-owned Analytics semantics."""
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi import HTTPException
from pglast import parse_sql

from app import usage_access


SCHEMA = (Path(__file__).resolve().parents[1] / "docs" / "analytics-reporting-schema.sql").read_text(
    encoding="utf-8"
)


class Result:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class Metadata:
    def __init__(self, row):
        self.row = row
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if sql == "SET TRANSACTION READ ONLY":
            return Result(None)
        return Result(self.row)


def install_metadata(monkeypatch, row):
    database = Metadata(row)

    @contextmanager
    def connection():
        yield database

    monkeypatch.setattr(usage_access, "get_metadata_conn", connection)
    return database


def test_authorization_uses_only_database_semantic_views(monkeypatch):
    database = install_metadata(monkeypatch, (True, True))
    identity = {"client_key": "srp", "sub": "provider-oid", "email": "ADMIN@example.test"}
    assert usage_access.require_usage_reporting_access(identity, "srp") is True
    sql, params = database.calls[-1]
    assert "analytics_reporting.effective_grants" in sql
    assert "analytics_reporting.effective_assignments" in sql
    assert "security_user_roles" not in sql
    assert "security_group_members" not in sql
    assert params["subject"] == "provider-oid"
    assert params["email"] == "ADMIN@example.test"
    assert params["permission"] == "usage:read"


def test_database_semantics_can_authorize_client_reporter_without_admin_scope(monkeypatch):
    install_metadata(monkeypatch, (True, False))
    monkeypatch.setenv("SRP_BICYCLE_ADMIN_ROLE", "srpqa_admin")
    monkeypatch.setenv("SRP_CORPORATE_ANALYTICS_ROLE", "srpqa_bci_analytics")
    identity = {"client_key": "srp", "sub": "corporate-user", "email": "corporate@example.test"}
    assert usage_access.require_analytics_reporting_access(identity, "srp", "access") is False


def test_database_denial_remains_fail_closed(monkeypatch):
    install_metadata(monkeypatch, (False, True))
    with pytest.raises(HTTPException) as denied:
        usage_access.require_usage_reporting_access(
            {"client_key": "srp", "sub": "synthetic-user"}, "srp"
        )
    assert denied.value.status_code == 403


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
