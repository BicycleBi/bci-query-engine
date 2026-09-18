import pytest
from fastapi import HTTPException

from app.analytics import get_analytics_reporting_config


def test_generic_reporting_config_is_client_scoped(monkeypatch):
    monkeypatch.setenv("ANALYTICS_REPORTING_CLIENT_KEY", "rag")
    monkeypatch.setenv("ANALYTICS_BICYCLE_ADMIN_ROLE", "ragdev_admin")
    monkeypatch.setenv("ANALYTICS_CLIENT_REPORTING_ROLE", "ragdev_analytics")

    config = get_analytics_reporting_config("rag")
    assert config.client_key == "rag"
    assert config.admin_role == "ragdev_admin"
    assert config.client_reporting_role == "ragdev_analytics"
    assert config.audiences == {"all", "rag", "bicycle"}

    with pytest.raises(HTTPException) as mismatch:
        get_analytics_reporting_config("rbp")
    assert mismatch.value.status_code == 503


def test_partial_generic_reporting_config_fails_closed(monkeypatch):
    monkeypatch.setenv("ANALYTICS_REPORTING_CLIENT_KEY", "rbp")
    with pytest.raises(HTTPException) as incomplete:
        get_analytics_reporting_config("rbp")
    assert incomplete.value.status_code == 503


def test_srp_legacy_role_names_remain_an_explicit_compatibility_path(monkeypatch):
    monkeypatch.setenv("SRP_BICYCLE_ADMIN_ROLE", "srpqa_admin")
    monkeypatch.setenv("SRP_CORPORATE_ANALYTICS_ROLE", "srpqa_analytics")

    config = get_analytics_reporting_config("srp")
    assert config.admin_role == "srpqa_admin"
    assert config.client_reporting_role == "srpqa_analytics"
    assert config.audiences == {"all", "srp", "bicycle"}


def test_optional_context_never_reuses_another_clients_roles(monkeypatch):
    monkeypatch.setenv("ANALYTICS_REPORTING_CLIENT_KEY", "rf")
    monkeypatch.setenv("ANALYTICS_BICYCLE_ADMIN_ROLE", "rfdev_admin")
    assert get_analytics_reporting_config("rag", required=False) is None
