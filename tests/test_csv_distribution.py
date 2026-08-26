import csv
import importlib
import sys
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

sys.modules.setdefault("psycopg", SimpleNamespace(connect=None, Connection=object))
sys.modules.setdefault("requests", SimpleNamespace(post=None))

engine = importlib.import_module("app.engine")
models = importlib.import_module("app.models")


class FakeResult:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class ArtifactGroupMeta:
    def __init__(self, groups):
        self.groups = groups
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if "FROM app.artifacts a" in sql:
            return FakeResult([(
                "11111111-1111-1111-1111-111111111111",
                "rf",
                "market-penetration-fdh-csv-burst",
                "public.market_penetration_fdh_csv_burst_render_payload",
                "email",
                "Market Penetration FDH CSV Burst",
                "<p>body</p>",
                "22222222-2222-2222-2222-222222222222",
            )])
        if "FROM app.distribution_groups distribution_group" in sql:
            return FakeResult(self.groups)
        raise AssertionError(f"Unexpected metadata query: {sql}")


class DistributionMeta:
    def __init__(self, approved, members):
        self.approved = approved
        self.members = members
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if "SELECT distribution_group.group_key" in sql:
            return FakeResult([(key,) for key in self.approved])
        if "SELECT contact.email, member.delivery_type" in sql:
            return FakeResult(self.members)
        raise AssertionError(f"Unexpected metadata query: {sql}")


def test_csv_output_uses_database_columns_and_neutralizes_formulas(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTIFACT_OUTPUT_DIR", str(tmp_path))
    contract = {
        "csv": {
            "filename": "filtered-fdhs.csv",
            "columns": [
                {"key": "fdh", "label": "FDH"},
                {"key": "active", "label": "Active Services"},
            ],
            "rows": [
                {"fdh": "=HYPERLINK(\"https://invalid.example\")", "active": 12},
                {"fdh": "  +1", "active": 3},
                {"fdh": "FDH-2", "active": 0},
            ],
        }
    }

    output = engine._generate_csv_output(
        run_id="run-1",
        artifact_id="artifact-1",
        client_key="rf",
        artifact_key="market-penetration-fdh-csv-burst",
        query_result=contract,
    )

    with open(output["storage_path"], encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))

    assert rows == [
        ["FDH", "Active Services"],
        ["'=HYPERLINK(\"https://invalid.example\")", "12"],
        ["'  +1", "3"],
        ["FDH-2", "0"],
    ]
    assert output["output_format"] == "csv"
    assert output["output_role"] == "attachment"
    assert output["content_type"] == "text/csv; charset=utf-8"
    assert output["file_size_bytes"] > 0


def test_csv_output_requires_database_owned_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTIFACT_OUTPUT_DIR", str(tmp_path))

    with pytest.raises(ValueError, match="database-owned csv contract"):
        engine._generate_csv_output(
            run_id="run-1",
            artifact_id="artifact-1",
            client_key="rf",
            artifact_key="market-penetration-fdh-csv-burst",
            query_result={"rows": []},
        )


def test_distribution_groups_require_artifact_approval_and_deduplicate_members():
    meta = DistributionMeta(
        approved=["group-a", "group-b"],
        members=[
            ("one@example.com", "cc"),
            ("ONE@example.com", "to"),
            ("two@example.com", "bcc"),
        ],
    )

    recipients = engine._resolve_distribution_recipients(
        meta,
        artifact_id="11111111-1111-1111-1111-111111111111",
        client_key="rf",
        group_keys=["group-a", "group-b"],
    )

    assert recipients == [("ONE@example.com", "to"), ("two@example.com", "bcc")]
    assert len(meta.calls) == 2


def test_unapproved_distribution_group_is_rejected_before_member_lookup():
    meta = DistributionMeta(approved=["group-a"], members=[])

    with pytest.raises(ValueError, match="group-b"):
        engine._resolve_distribution_recipients(
            meta,
            artifact_id="11111111-1111-1111-1111-111111111111",
            client_key="rf",
            group_keys=["group-a", "group-b"],
        )

    assert len(meta.calls) == 1


def test_approved_distribution_group_list_exposes_labels_without_recipients(monkeypatch):
    meta = ArtifactGroupMeta([
        ("operations", "Operations", "Operations audience"),
        ("sales", "Sales", None),
    ])
    monkeypatch.setattr(engine, "get_metadata_conn", lambda: meta)

    groups = engine.get_artifact_distribution_groups(
        "rf",
        "market-penetration-fdh-csv-burst",
    )

    assert groups == [
        {
            "group_key": "operations",
            "display_name": "Operations",
            "description": "Operations audience",
        },
        {
            "group_key": "sales",
            "display_name": "Sales",
            "description": None,
        },
    ]
    group_sql = meta.calls[1][0]
    assert "distribution_group_members" in group_sql
    assert "contact.email" not in group_sql


def test_execution_request_requires_query_for_csv_and_delivery_for_groups():
    with pytest.raises(ValidationError, match="CSV output requires"):
        models.ArtifactExecutionRequest(
            client_key="rf",
            artifact_key="market-penetration-fdh-csv-burst",
            output_formats=["csv"],
        )

    with pytest.raises(ValidationError, match="supported only for delivery"):
        models.ArtifactExecutionRequest(
            client_key="rf",
            artifact_key="market-penetration-fdh-csv-burst",
            behavior="display",
            query={"action": "hierarchy-export"},
            distribution_group_keys=["market-penetration-dev-test"],
        )
