import base64
import hashlib
import hmac
import importlib
import json
import sys
import time
from datetime import datetime, timezone

from fastapi.testclient import TestClient


def _load_main(monkeypatch):
    monkeypatch.setenv("QUERY_ENGINE_SECURITY_TOKEN_SECRET", "test-secret")
    monkeypatch.setenv("QUERY_ENGINE_SECURITY_TOKEN_ISSUER", "bci-security")
    monkeypatch.setenv("QUERY_ENGINE_SECURITY_TOKEN_AUDIENCE", "bci-client")

    for name in list(sys.modules):
        if name == "app.main" or name.startswith("app.main."):
            sys.modules.pop(name, None)

    module = importlib.import_module("app.main")
    return module


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


def test_health_is_public(monkeypatch):
    main = _load_main(monkeypatch)
    client = TestClient(main.app)

    response = client.get("/health")
    assert response.status_code == 200


def test_protected_routes_require_internal_token(monkeypatch):
    main = _load_main(monkeypatch)
    client = TestClient(main.app)

    response = client.post(
        "/artifact-executions",
        json={
            "client_key": "srp",
            "artifact_key": "visit-counts",
            "behavior": "display",
        },
    )
    assert response.status_code == 401


def test_protected_routes_accept_valid_internal_token(monkeypatch):
    main = _load_main(monkeypatch)
    client = TestClient(main.app)
    captured = {}

    def fake_execute_artifact(
        client_key,
        artifact_key,
        behavior,
        output_formats,
        authenticated_subject,
        authorized_roles,
    ):
        captured["authenticated_subject"] = authenticated_subject
        captured["authorized_roles"] = authorized_roles
        captured["output_formats"] = output_formats
        return {
            "run_id": "run-1",
            "status": "success",
            "client_key": client_key,
            "artifact_key": artifact_key,
            "started_at": datetime.now(tz=timezone.utc),
            "completed_at": datetime.now(tz=timezone.utc),
            "behavior": behavior,
            "preview_html": "<p>ok</p>",
        }

    monkeypatch.setattr(
        main,
        "execute_artifact",
        fake_execute_artifact,
    )
    token = _encode_token(
        {
            "aud": "bci-client",
            "client_key": "srp",
            "display_name": "Security Service",
            "email": "security@bicyclebi.com",
            "exp": int(time.time()) + 3600,
            "iat": int(time.time()),
            "iss": "bci-security",
            "roles": ["developer"],
            "session_id": "session-1",
            "sub": "user-1",
        }
    )

    response = client.post(
        "/artifact-executions",
        headers={
            **_auth_headers(token),
            "X-Identity-Roles": "srp_pnl_scope_b,srp_pnl_scope_a",
        },
        json={
            "client_key": "srp",
            "artifact_key": "visit-counts",
            "behavior": "display",
        },
    )
    assert response.status_code == 202
    assert response.json()["preview_html"] == "<p>ok</p>"
    assert captured["authenticated_subject"] == "user-1"
    assert captured["authorized_roles"] == ["srp_pnl_scope_a", "srp_pnl_scope_b"]
    assert captured["output_formats"] == []


def test_artifact_data_route_passes_trusted_authorization_context(monkeypatch):
    main = _load_main(monkeypatch)
    client = TestClient(main.app)
    captured = {}

    def fake_fetch_artifact_data(client_key, artifact_key, **kwargs):
        captured.update(kwargs)
        return {"rows": [], "client_key": client_key, "artifact_key": artifact_key}

    monkeypatch.setattr(main, "fetch_artifact_data", fake_fetch_artifact_data)
    token = _encode_token(
        {
            "aud": "bci-client",
            "client_key": "srp",
            "exp": int(time.time()) + 3600,
            "iat": int(time.time()),
            "iss": "bci-security",
            "roles": ["stale-token-role"],
            "sub": "user-1",
        }
    )

    response = client.get(
        "/artifacts/srp/pricing-insights/data",
        headers={
            **_auth_headers(token),
            "X-Identity-Roles": "srp_scope_b,srp_scope_a",
        },
    )

    assert response.status_code == 200
    assert captured["authenticated_subject"] == "user-1"
    assert captured["authorized_roles"] == ["srp_scope_a", "srp_scope_b"]


def test_protected_routes_reject_invalid_authorization_roles(monkeypatch):
    main = _load_main(monkeypatch)
    client = TestClient(main.app)
    token = _encode_token(
        {
            "aud": "bci-client",
            "client_key": "srp",
            "exp": int(time.time()) + 3600,
            "iat": int(time.time()),
            "iss": "bci-security",
            "roles": "srp_pnl_scope_a",
            "sub": "user-1",
        }
    )

    response = client.post(
        "/artifact-executions",
        headers=_auth_headers(token),
        json={
            "client_key": "srp",
            "artifact_key": "visit-counts",
            "behavior": "display",
        },
    )
    assert response.status_code == 403


def test_blank_forwarded_roles_fall_back_to_signed_token_roles(monkeypatch):
    main = _load_main(monkeypatch)

    assert main.authorized_roles(
        {"roles": ["srp_production_admin"]},
        "",
    ) == ["srp_production_admin"]


def test_identity_without_any_roles_is_forbidden(monkeypatch):
    main = _load_main(monkeypatch)
    client = TestClient(main.app)
    token = _encode_token(
        {
            "aud": "bci-client",
            "client_key": "srp",
            "exp": int(time.time()) + 3600,
            "iat": int(time.time()),
            "iss": "bci-security",
            "roles": [],
            "sub": "user-1",
        }
    )

    response = client.post(
        "/artifact-executions",
        headers={
            **_auth_headers(token),
            "X-Identity-Roles": "",
        },
        json={
            "client_key": "srp",
            "artifact_key": "visit-counts",
            "behavior": "display",
        },
    )
    assert response.status_code == 403


def test_protected_routes_reject_token_without_subject(monkeypatch):
    main = _load_main(monkeypatch)
    client = TestClient(main.app)
    token = _encode_token(
        {
            "aud": "bci-client",
            "client_key": "srp",
            "exp": int(time.time()) + 3600,
            "iat": int(time.time()),
            "iss": "bci-security",
        }
    )

    response = client.post(
        "/artifact-executions",
        headers=_auth_headers(token),
        json={
            "client_key": "srp",
            "artifact_key": "visit-counts",
            "behavior": "display",
        },
    )
    assert response.status_code == 403


def test_protected_routes_reject_wrong_client_token(monkeypatch):
    main = _load_main(monkeypatch)
    client = TestClient(main.app)
    token = _encode_token(
        {
            "aud": "bci-client",
            "client_key": "other-client",
            "exp": int(time.time()) + 3600,
            "iat": int(time.time()),
            "iss": "bci-security",
            "sub": "user-1",
        }
    )

    response = client.post(
        "/artifact-executions",
        headers=_auth_headers(token),
        json={
            "client_key": "srp",
            "artifact_key": "visit-counts",
            "behavior": "display",
        },
    )
    assert response.status_code == 403


def test_protected_routes_reject_token_without_client_scope(monkeypatch):
    main = _load_main(monkeypatch)
    client = TestClient(main.app)
    token = _encode_token(
        {
            "aud": "bci-client",
            "exp": int(time.time()) + 3600,
            "iat": int(time.time()),
            "iss": "bci-security",
            "sub": "user-1",
        }
    )

    response = client.post(
        "/artifact-executions",
        headers=_auth_headers(token),
        json={
            "client_key": "srp",
            "artifact_key": "visit-counts",
            "behavior": "display",
        },
    )
    assert response.status_code == 403
