"""Synthetic-only tests for configuration-driven facility reporting."""

from fastapi import HTTPException
from fastapi.testclient import TestClient
import inspect
import pytest

from app import facility_reporting as reporting
from test_usage_summary import _auth_headers, _load_main, _token


def configuration():
    return {
        "facilities": [("ecw:1", "1", "Example North"), ("ecw:2", "2", "Example South")],
        "activity": [("ecw:1", "available"), ("ecw:2", "no_data")],
        "report_scope": [
            ("ecw:1", "1", "Example North", "example-report", "entity_north", "maintained_scope"),
        ],
        "distributions": [
            ("ecw:1", "Example North", "example-report", "example-delivery", "north@example.invalid", "to", True, "maintained_recipient"),
        ],
        "role_scope": [
            ("ecw:1", "Example North", "example-report", "example_role", "entity_north", "maintained_role_scope"),
        ],
    }


def test_rows_use_only_maintained_configuration():
    catalog = {
        "example-report": {
            "artifact_key": "example-report",
            "display_name": "Example Report",
            "delivery_mode": "both",
            "front_end": True,
            "active": True,
        }
    }
    users = [("Example User", "user@example.invalid", "example-report", "example_role", "direct", None)]
    rows = reporting._build_rows(configuration(), catalog, users)
    assert [row["facility_name"] for row in rows] == ["Example North", "Example South"]
    north = rows[0]
    assert north["entity_codes"] == ["entity_north"]
    assert north["reports"][0]["mapping_sources"] == ["maintained_scope"]
    assert north["reports"][0]["distributions"][0]["recipient_email"] == "north@example.invalid"
    assert north["reports"][0]["users"][0]["username"] == "user@example.invalid"
    assert rows[1]["reports"] == []
    assert north["operational_data"] == "available" and north["facility_status"] == "active"
    assert rows[1]["facility_status"] == "inactive"


def test_disabled_or_unregistered_configuration_is_not_invented():
    rows = reporting._build_rows(configuration(), {}, [])
    assert all(row["reports"] == [] for row in rows)


def test_copied_production_evidence_replaces_dev_placeholders():
    catalog = {
        "example-report": {
            "artifact_key": "example-report",
            "display_name": "Example Report",
            "delivery_mode": "both",
            "front_end": True,
            "active": True,
        }
    }
    live = {"rows": reporting._build_rows(configuration(), catalog, []), "sources": []}
    access = {
        "source_environment": "production",
        "captured_at": "2026-11-11T00:00:00+00:00",
        "rows": [{
            "active": True,
            "display_name": "Example User",
            "username": "user@example.invalid",
            "matches": [{
                "artifact_key": "example-report",
                "artifact_active": True,
                "can_view": True,
                "can_run": False,
                "details": [{"role": "example_role", "source": "direct"}],
            }],
        }],
    }
    distributions = {
        "source_environment": "production",
        "rows": [{
            "configuration_state": "enabled",
            "report_artifact_key": "example-report",
            "delivery_artifact_key": "example-report-north-email",
            "recipient_email": "delivery@example.invalid",
            "delivery_type": "to",
            "facilities": ["Example North"],
            "source": "production_snapshot",
        }],
    }
    activity = {
        "source_environment": "production",
        "rows": [
            {"facility_key": "ecw:1", "facility_name": "Example North", "operational_data": "available"},
            {"facility_key": "ecw:2", "facility_name": "Example South", "operational_data": "no_data"},
        ],
    }
    result = reporting.attach_copied_facility_evidence(
        live,
        access_payload=access,
        distribution_payload=distributions,
        activity_payload=activity,
        home_artifacts={"example-report"},
    )
    north, south = result["rows"]
    assert north["facility_status"] == "active"
    assert south["facility_status"] == "inactive"
    assert north["reports"][0]["distributions"][0]["recipient_email"] == "delivery@example.invalid"
    assert north["reports"][0]["users"][0]["username"] == "user@example.invalid"
    assert north["reports"][0]["users"][0]["available_from_home"] is True
    assert "_access_roles" not in north["reports"][0]
    assert "facility_id" not in north


def test_live_activity_is_preserved_without_a_copied_snapshot():
    catalog = {
        "example-report": {
            "artifact_key": "example-report",
            "display_name": "Example Report",
            "delivery_mode": "web",
            "front_end": True,
            "active": True,
        }
    }
    live = {"rows": reporting._build_rows(configuration(), catalog, []), "sources": [
        {"source": "activity", "status": "available"},
        {"source": "distributions", "status": "available"},
        {"source": "security_metadata", "status": "available"},
    ]}
    result = reporting.attach_copied_facility_evidence(live)
    north, south = result["rows"]
    assert north["facility_status"] == "active"
    assert south["facility_status"] == "inactive"
    assert {source["source"] for source in result["sources"]} >= {
        "facility_access", "facility_distribution", "facility_activity"
    }


def test_report_bounds_search_and_status_rejection(monkeypatch):
    data = configuration()
    monkeypatch.setattr(reporting, "_load_configuration", lambda: (data, []))
    monkeypatch.setattr(reporting, "_artifact_catalog", lambda keys: ({
        "example-report": {"artifact_key": "example-report", "display_name": "Example Report", "delivery_mode": "web", "front_end": True, "active": True}
    }, [("example-report",)]))
    monkeypatch.setattr(reporting, "_scoped_users", lambda keys: [])
    monkeypatch.setattr(reporting, "_home_artifacts", lambda: (set(), []))
    result = reporting.get_facility_report(search="north")
    assert result["total"] == 1 and result["coverage"] == "configuration"
    assert result["rows"][0]["facility_name"] == "Example North"
    with pytest.raises(ValueError):
        reporting.get_facility_report(status="active")
    with pytest.raises(ValueError):
        reporting.get_facility_report(offset=-1)


def test_source_contract_contains_no_operational_fact_scans():
    queries = inspect.getsource(reporting._load_configuration).casefold()
    assert "appointment" not in queries
    assert "claim" not in queries
    assert "srp_client_access_facility_report_scope" in queries
    assert "srp_client_access_facility_distribution" in queries


def test_facility_route_preserves_existing_authorization_and_no_store(monkeypatch):
    main = _load_main(monkeypatch)
    calls = []
    monkeypatch.setattr(main, "require_analytics_reporting_access", lambda *args: calls.append(args))
    monkeypatch.setattr(main, "get_facility_report", lambda **kw: {"client_key": "srp", "audience": "srp", "rows": []})
    monkeypatch.setattr(main, "get_copied_snapshot", lambda *args: None)
    monkeypatch.setattr(main, "attach_distribution_catalog", lambda value: value)
    monkeypatch.setattr(main, "attach_copied_facility_evidence", lambda value, **kwargs: value)
    monkeypatch.setattr(main, "attach_report_configurations", lambda value, **kwargs: value)
    monkeypatch.setattr(main, "get_home_artifact_keys", lambda *args: [])
    client = TestClient(main.app)
    path = "/artifacts/srp/client-access-distribution-report/facility-summary"
    assert client.get(path).status_code == 401
    assert client.get(path, headers=_auth_headers(_token("rf"))).status_code == 403
    response = client.get(path, headers=_auth_headers(_token("srp")))
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert calls[0][1:] == ("srp", "access")

    def denied(*args):
        raise HTTPException(403, "denied")

    monkeypatch.setattr(main, "require_analytics_reporting_access", denied)
    monkeypatch.setattr(main, "get_facility_report", lambda **kw: pytest.fail("source read before authorization"))
    assert client.get(path, headers=_auth_headers(_token("srp"))).status_code == 403
