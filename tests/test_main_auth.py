import base64
import hashlib
import hmac
import importlib
import json
import sys
import time
from datetime import datetime, timezone
from uuid import UUID

from fastapi.testclient import TestClient


def _load_main(monkeypatch):
    monkeypatch.setenv("QUERY_ENGINE_SECURITY_TOKEN_SECRET", "test-secret")
    monkeypatch.setenv("QUERY_ENGINE_SECURITY_TOKEN_ISSUER", "bci-security")
    monkeypatch.setenv("QUERY_ENGINE_SECURITY_TOKEN_AUDIENCE", "bci-client")
    monkeypatch.setenv("SERVICE_TOKEN", "test-service-token")

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


def test_delivery_execution_returns_queued_run_and_uses_background_task(monkeypatch):
    main = _load_main(monkeypatch)
    client = TestClient(main.app)
    captured = {}
    started_at = datetime.now(tz=timezone.utc)

    monkeypatch.setattr(
        main,
        "queue_artifact_execution",
        lambda client_key, artifact_key: {
            "run_id": "11111111-1111-1111-1111-111111111111",
            "client_key": client_key,
            "artifact_key": artifact_key,
            "status": "queued",
            "started_at": started_at,
            "completed_at": None,
            "outputs": [],
        },
    )

    def fake_execute_artifact(client_key, artifact_key, **kwargs):
        captured.update(
            client_key=client_key,
            artifact_key=artifact_key,
            **kwargs,
        )
        return {
            "run_id": kwargs["run_id"],
            "client_key": client_key,
            "artifact_key": artifact_key,
            "status": "success",
            "started_at": kwargs["started_at"],
            "completed_at": datetime.now(tz=timezone.utc),
            "outputs": [],
        }

    monkeypatch.setattr(main, "execute_queued_artifact", fake_execute_artifact)
    token = _encode_token(
        {
            "aud": "bci-client",
            "client_key": "srp",
            "exp": int(time.time()) + 3600,
            "iat": int(time.time()),
            "iss": "bci-security",
            "roles": ["developer"],
            "sub": "user-1",
        }
    )

    response = client.post(
        "/artifact-executions",
        headers={
            **_auth_headers(token),
            "X-Identity-Roles": "srp_pnl_scope_a",
        },
        json={
            "client_key": "srp",
            "artifact_key": "visit-counts",
            "behavior": "deliver",
        },
    )

    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    assert response.json()["run_id"] == "11111111-1111-1111-1111-111111111111"
    assert captured["precreated_run"] is True
    assert captured["authenticated_subject"] == "user-1"
    assert captured["authorized_roles"] == ["srp_pnl_scope_a"]


def test_execution_status_rejects_other_client_run(monkeypatch):
    main = _load_main(monkeypatch)
    client = TestClient(main.app)
    monkeypatch.setattr(
        main,
        "get_run",
        lambda run_id: {
            "run_id": run_id,
            "client_key": "rag",
            "artifact_key": "pricing-insights",
            "status": "queued",
            "started_at": datetime.now(tz=timezone.utc),
            "completed_at": None,
            "outputs": [],
        },
    )
    token = _encode_token(
        {
            "aud": "bci-client",
            "client_key": "srp",
            "exp": int(time.time()) + 3600,
            "iat": int(time.time()),
            "iss": "bci-security",
            "roles": ["developer"],
            "sub": "user-1",
        }
    )

    response = client.get(
        "/artifact-executions/11111111-1111-1111-1111-111111111111",
        headers=_auth_headers(token),
    )

    assert response.status_code == 403


def test_execution_status_serializes_database_uuid(monkeypatch):
    main = _load_main(monkeypatch)
    client = TestClient(main.app)
    run_id = UUID("33333333-3333-3333-3333-333333333333")
    monkeypatch.setattr(
        main,
        "get_run",
        lambda requested_run_id: {
            "run_id": run_id,
            "client_key": "srp",
            "artifact_key": "visit-counts",
            "status": "completed",
            "started_at": datetime.now(tz=timezone.utc),
            "completed_at": datetime.now(tz=timezone.utc),
            "outputs": [],
        },
    )
    token = _encode_token(
        {
            "aud": "bci-client",
            "client_key": "srp",
            "exp": int(time.time()) + 3600,
            "iat": int(time.time()),
            "iss": "bci-security",
            "roles": ["developer"],
            "sub": "user-1",
        }
    )

    response = client.get(
        f"/artifact-executions/{run_id}",
        headers=_auth_headers(token),
    )

    assert response.status_code == 200
    assert response.json()["run_id"] == str(run_id)
    assert response.json()["status"] == "completed"


def test_artifact_data_route_passes_trusted_authorization_context(monkeypatch):
    main = _load_main(monkeypatch)
    client = TestClient(main.app)
    captured = {}

    def fake_execute_artifact_query(client_key, artifact_key, **kwargs):
        captured.update(kwargs)
        return {"rows": [], "client_key": client_key, "artifact_key": artifact_key}

    monkeypatch.setattr(main, "execute_artifact_query", fake_execute_artifact_query)
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

    response = client.post(
        "/artifacts/srp/pricing-insights/data",
        headers={
            **_auth_headers(token),
            "X-Identity-Roles": "srp_scope_b,srp_scope_a",
        },
        json={
            "operation": "details",
            "filters": {"type": "Vehicles & Transport"},
        },
    )

    assert response.status_code == 200
    assert captured["query"] == {
        "operation": "details",
        "filters": {"type": "Vehicles & Transport"},
    }
    assert captured["authenticated_subject"] == "user-1"
    assert captured["authorized_roles"] == ["srp_scope_a", "srp_scope_b"]


def test_artifact_query_cache_prewarm_requires_service_token(monkeypatch):
    main = _load_main(monkeypatch)
    client = TestClient(main.app)

    response = client.post(
        "/internal/artifacts/rag/lot-summary/query-cache/prewarm",
        json={"queries": [{"operation": "dataset", "dataset": "summary"}]},
    )

    assert response.status_code == 401


def test_artifact_query_cache_prewarm_uses_service_only_contract(monkeypatch):
    main = _load_main(monkeypatch)
    client = TestClient(main.app)
    captured = {}

    def fake_prewarm(client_key, artifact_key, **kwargs):
        captured.update(kwargs)
        return {
            "client_key": client_key,
            "artifact_key": artifact_key,
            "status": "prewarmed",
            "entry_count": len(kwargs["queries"]),
        }

    monkeypatch.setattr(main, "prewarm_artifact_query_cache", fake_prewarm)
    queries = [
        {"operation": "dataset", "dataset": "summary"},
        {"operation": "dataset", "dataset": "details"},
    ]
    response = client.post(
        "/internal/artifacts/rag/lot-summary/query-cache/prewarm",
        headers={"Authorization": "Bearer test-service-token"},
        json={"queries": queries},
    )

    assert response.status_code == 200
    assert response.json() == {
        "client_key": "rag",
        "artifact_key": "lot-summary",
        "status": "prewarmed",
        "entry_count": 2,
    }
    assert captured["queries"] == queries


def test_artifact_asset_route_is_authenticated_and_immutable(monkeypatch):
    main = _load_main(monkeypatch)
    client = TestClient(main.app)

    monkeypatch.setattr(
        main,
        "get_artifact_asset",
        lambda client_key, artifact_key, asset_path: {
            "content": b"window.assetReady=true;",
            "content_type": "application/javascript",
            "sha256": "a" * 64,
        },
    )
    token = _encode_token(
        {
            "aud": "bci-client",
            "client_key": "rag",
            "exp": int(time.time()) + 3600,
            "iat": int(time.time()),
            "iss": "bci-security",
            "roles": ["rag-user"],
            "sub": "user-1",
        }
    )

    response = client.get(
        "/artifacts/rag/lot-summary/assets/echarts.abc123.min.js",
        headers=_auth_headers(token),
    )

    assert response.status_code == 200
    assert response.content == b"window.assetReady=true;"
    assert response.headers["content-type"].startswith("application/javascript")
    assert response.headers["cache-control"] == "private, max-age=31536000, immutable"
    assert response.headers["etag"] == f'"{"a" * 64}"'


def test_artifact_asset_route_rejects_other_client(monkeypatch):
    main = _load_main(monkeypatch)
    client = TestClient(main.app)
    token = _encode_token(
        {
            "aud": "bci-client",
            "client_key": "srp",
            "exp": int(time.time()) + 3600,
            "iat": int(time.time()),
            "iss": "bci-security",
            "roles": ["srp-user"],
            "sub": "user-1",
        }
    )

    response = client.get(
        "/artifacts/rag/lot-summary/assets/echarts.abc123.min.js",
        headers=_auth_headers(token),
    )

    assert response.status_code == 403


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
