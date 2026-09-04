import base64
import hashlib
import hmac
import importlib
import json
import sys
import time

from fastapi.testclient import TestClient


def _load_main(monkeypatch):
    monkeypatch.setenv("QUERY_ENGINE_SECURITY_TOKEN_SECRET", "test-secret")
    monkeypatch.setenv("QUERY_ENGINE_SECURITY_TOKEN_ISSUER", "bci-security")
    monkeypatch.setenv("QUERY_ENGINE_SECURITY_TOKEN_AUDIENCE", "bci-client")
    monkeypatch.setenv("SERVICE_TOKEN", "test-service-token")
    sys.modules.pop("app.main", None)
    return importlib.import_module("app.main")


def _encode_token(payload, secret="test-secret"):
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).digest()
    return (
        base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")
        + "."
        + base64.urlsafe_b64encode(signature).decode("utf-8").rstrip("=")
    )


def _auth_headers(token):
    return {"Authorization": f"Bearer {token}"}


def _token(client_key="rf"):
    return _encode_token(
        {
            "aud": "bci-client",
            "client_key": client_key,
            "display_name": "Jeanre",
            "email": "jeanre@example.test",
            "exp": int(time.time()) + 3600,
            "iat": int(time.time()),
            "iss": "bci-security",
            "roles": ["developer"],
            "sub": "user-1",
        }
    )


def _summary(client_key, days):
    return {
        "client_key": client_key,
        "days": days,
        "period_start": "2026-08-05T00:00:00Z",
        "period_end": "2026-09-04T00:00:00Z",
        "totals": {"requests": 2, "active_users": 1},
        "daily": [],
        "artifacts": [],
        "users": [{"display_name": "Jeanre", "username": "jeanre@example.test", "requests": 2}],
        "login_outcomes": [],
    }


def test_usage_summary_is_authenticated_and_client_scoped(monkeypatch):
    main = _load_main(monkeypatch)
    captured = []
    monkeypatch.setattr(
        main,
        "get_usage_summary",
        lambda *, client_key, days: captured.append((client_key, days)) or _summary(client_key, days),
    )
    client = TestClient(main.app)

    path = "/artifacts/rf/usage-monitoring-dashboard/usage-summary?days=30"
    assert client.get(path).status_code == 401
    assert client.get(
        path, headers=_auth_headers(_token("srp"))
    ).status_code == 403

    response = client.get(
        path, headers=_auth_headers(_token("rf"))
    )
    assert response.status_code == 200
    assert response.json()["totals"]["requests"] == 2
    assert response.json()["users"][0]["display_name"] == "Jeanre"
    assert captured == [("rf", 30)]


def test_usage_summary_rejects_unbounded_period(monkeypatch):
    main = _load_main(monkeypatch)
    client = TestClient(main.app)
    response = client.get(
        "/artifacts/rf/usage-monitoring-dashboard/usage-summary?days=365",
        headers=_auth_headers(_token("rf")),
    )
    assert response.status_code == 400


def test_usage_summary_is_available_only_for_the_monitoring_artifact(monkeypatch):
    main = _load_main(monkeypatch)
    client = TestClient(main.app)
    response = client.get(
        "/artifacts/rf/market-penetration-dashboard/usage-summary?days=30",
        headers=_auth_headers(_token("rf")),
    )
    assert response.status_code == 404


def test_usage_summary_contract_excludes_sensitive_identifiers():
    monitoring = importlib.import_module("app.monitoring")
    source = importlib.import_module("inspect").getsource(monitoring.get_usage_summary)
    for forbidden in ("email", "request_id", "session_id", "response_body", "request_body"):
        assert f'"{forbidden}"' not in source
