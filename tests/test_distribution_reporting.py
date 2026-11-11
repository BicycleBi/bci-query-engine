"""Synthetic-only tests for live distribution configuration reporting."""

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

from app import distribution_reporting as reporting
from test_usage_summary import _auth_headers, _load_main, _token


def approval(**extra):
    return dict(facilities=["north"], delivery_type="to", approval_source="synthetic approval",
                approver="Example Reviewer", approval_date="2026-10-27", maintenance_owner="Example Owner", **extra)


@pytest.mark.parametrize("configured,approved,expected", [
    ({"enabled": True, "facilities": ["north"], "facility_resolved": True, "delivery_type": "to"}, None, "not_confirmed"),
    (None, approval(), "missing"),
    ({"enabled": True, "facilities": ["north"], "facility_resolved": True, "delivery_type": "to"}, approval(), "matches"),
])
def test_comparison_requires_explicit_approval(configured, approved, expected):
    assert reporting.compare_assignment(configured, approved) == expected


def test_distribution_rows_are_grouped_from_live_projection(monkeypatch):
    def read(factory, relation, query, params=None):
        if relation == "public.srp_client_access_facility_distribution":
            return [
                ("Example North", "example-report", "delivery-north", "list@example.invalid", "to", True, "maintained"),
                ("Example South", "example-report", "delivery-north", "list@example.invalid", "to", True, "maintained"),
            ]
        if relation == "app.artifacts":
            return [("example-report", "Example Report", True)]
        raise AssertionError(relation)

    monkeypatch.setattr(reporting, "_read", read)
    result = reporting.get_distribution_report()
    assert result["coverage"] == "configuration"
    assert result["total"] == 1
    assert result["rows"][0]["facilities"] == ["Example North", "Example South"]
    assert result["rows"][0]["recipient_email"] == "list@example.invalid"


def test_unavailable_projection_is_not_an_empty_result(monkeypatch):
    monkeypatch.setattr(reporting, "_read", lambda *args, **kwargs: None)
    with pytest.raises(RuntimeError):
        reporting.get_distribution_report()


def test_schedule_configuration_uses_live_load_mapping(monkeypatch):
    monkeypatch.setattr(reporting, "_read", lambda *args, **kwargs: [
        ("example-report", "example-load", "example-trigger", "09:30:00", "America/New_York", True, True)
    ])
    configuration = reporting.get_report_configurations()["example-report"]
    assert configuration["load_key"] == "example-load"
    assert configuration["schedules"][0]["source"] == "load_setup.trigger"
    assert configuration["schedules"][0]["display_time"].endswith(" CT")


def test_report_preserves_authorization_and_no_store(monkeypatch):
    main = _load_main(monkeypatch)
    calls = []
    monkeypatch.setattr(main, "require_analytics_reporting_access", lambda *args: calls.append(args))
    monkeypatch.setattr(main, "get_distribution_report", lambda **kw: {"audience": "srp", "rows": []})
    client = TestClient(main.app)
    path = "/artifacts/srp/client-access-distribution-report/distribution-summary"
    assert client.get(path).status_code == 401
    assert client.get(path, headers=_auth_headers(_token("rf"))).status_code == 403
    response = client.get(path, headers=_auth_headers(_token("srp")))
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert calls[0][1:] == ("srp", "access")

    def denied(*args):
        raise HTTPException(403, "denied")

    monkeypatch.setattr(main, "require_analytics_reporting_access", denied)
    monkeypatch.setattr(main, "get_distribution_report", lambda **kw: pytest.fail("contacts read"))
    assert client.get(path, headers=_auth_headers(_token("srp"))).status_code == 403
