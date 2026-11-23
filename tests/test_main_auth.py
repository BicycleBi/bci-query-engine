import base64
import hashlib
import hmac
import importlib
import json
import sys
import time
from datetime import datetime, timezone

from fastapi.testclient import TestClient
from psycopg.errors import InsufficientPrivilege


def fake_grants(client, artifact, permission, subject, identity):
    from fastapi import HTTPException
    roles = identity.get("roles")
    if not roles:
        raise HTTPException(403, "Artifact access denied")
    return roles


def _load_main(monkeypatch):
    monkeypatch.setenv("QUERY_ENGINE_SECURITY_TOKEN_SECRET", "test-secret")
    monkeypatch.setenv("QUERY_ENGINE_SECURITY_TOKEN_ISSUER", "bci-security")
    monkeypatch.setenv("QUERY_ENGINE_SECURITY_TOKEN_AUDIENCE", "bci-client")
    monkeypatch.setenv("SERVICE_TOKEN", "test-service-token")

    for name in list(sys.modules):
        if name == "app.main" or name.startswith("app.main."):
            sys.modules.pop(name, None)

    module = importlib.import_module("app.main")
    monkeypatch.setattr(module, "permitted_roles", fake_grants)
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


def test_request_span_keeps_named_identity_and_correlation(monkeypatch):
    main = _load_main(monkeypatch)
    captured = []
    monkeypatch.setattr(main, "record_request_span_async", lambda **values: captured.append(values))
    monkeypatch.setattr(
        main,
        "execute_artifact",
        lambda *args, **kwargs: {
            "run_id": "11111111-1111-1111-1111-111111111111",
            "status": "success",
            "client_key": "rf",
            "artifact_key": "market-penetration",
            "started_at": datetime.now(tz=timezone.utc),
            "completed_at": datetime.now(tz=timezone.utc),
            "preview_html": "<p>ok</p>",
            "outputs": [],
            "cache": {"status": "hit", "data_query_ms": 4.5, "render_ms": 1.5},
        },
    )
    client = TestClient(main.app)
    token = _encode_token(
        {
            "aud": "bci-client",
            "client_key": "rf",
            "display_name": "Jeanre",
            "email": "jeanre@example.test",
            "exp": int(time.time()) + 3600,
            "iat": int(time.time()),
            "iss": "bci-security",
            "roles": ["developer"],
            "session_id": "11111111-1111-1111-1111-111111111112",
            "sub": "user-1",
        }
    )

    response = client.get(
        "/artifacts/rf/market-penetration",
        headers={**_auth_headers(token), "X-Request-ID": "gateway-request-1"},
    )

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "gateway-request-1"
    assert len(captured) == 1
    assert captured[0]["identity"]["email"] == "jeanre@example.test"
    assert captured[0]["identity"]["display_name"] == "Jeanre"
    assert captured[0]["route_template"] == "/artifacts/{client_key}/{artifact_key}"
    assert captured[0]["run_id"] == "11111111-1111-1111-1111-111111111111"


def test_interaction_endpoint_accepts_only_metadata(monkeypatch):
    main = _load_main(monkeypatch)
    captured = []
    monkeypatch.setattr(main, "record_interaction_event_async", lambda **values: captured.append(values))
    client = TestClient(main.app)
    token = _encode_token(
        {
            "aud": "bci-client",
            "client_key": "rf",
            "display_name": "Jeanre",
            "email": "jeanre@example.test",
            "exp": int(time.time()) + 3600,
            "iat": int(time.time()),
            "iss": "bci-security",
            "roles": ["developer"],
            "sub": "user-1",
        }
    )

    response = client.post(
        "/usage/interactions/rf/market-penetration",
        headers=_auth_headers(token),
        json={
            "client_key": "rf",
            "artifact_key": "market-penetration",
            "interaction_type": "filter_apply",
            "interaction_key": "region-selector",
            "duration_ms": 125,
        },
    )

    assert response.status_code == 202
    assert response.json() == {"status": "accepted"}
    assert captured[0]["event_type"] == "filter_apply"
    assert captured[0]["event_key"] == "region-selector"


def test_internal_usage_ingestion_requires_service_token_and_accepts_bounded_event(monkeypatch):
    main = _load_main(monkeypatch)
    captured = []
    monkeypatch.setattr(main, "record_ingested_event_async", lambda **values: captured.append(values))
    client = TestClient(main.app)
    event = {
        "source_service": "security",
        "event_type": "login_completed",
        "event_status": "succeeded",
        "client_key": "rf",
        "username": "Jeanre",
        "request_id": "login-123",
    }

    assert client.post("/internal/usage/events", json=event).status_code == 401
    response = client.post(
        "/internal/usage/events",
        headers={"Authorization": "Bearer test-service-token"},
        json=event,
    )

    assert response.status_code == 202
    assert response.json() == {"status": "accepted"}
    assert captured == [event | {
        "user_id": None,
        "email": None,
        "display_name": None,
        "session_id": None,
        "artifact_key": None,
        "run_id": None,
        "reason_code": None,
        "duration_ms": None,
        "http_status": None,
    }]


def test_internal_usage_ingestion_rejects_arbitrary_payload_fields(monkeypatch):
    main = _load_main(monkeypatch)
    client = TestClient(main.app)
    response = client.post(
        "/internal/usage/events",
        headers={"Authorization": "Bearer test-service-token"},
        json={
            "source_service": "security",
            "event_type": "login_denied",
            "event_status": "denied",
            "password": "must-not-be-accepted",
        },
    )
    assert response.status_code == 422


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
            "email": "reader@example.test",
        }
    )

    response = client.post(
        "/artifacts/srp/pricing-insights/data",
        headers={
            **_auth_headers(token),
            "X-Identity-Email": "other@example.test",
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
    assert captured["authenticated_email"] == "reader@example.test"
    assert captured["authorized_roles"] == ["stale-token-role"]

    def deny(*_args, **_kwargs):
        raise InsufficientPrivilege("private database detail")

    monkeypatch.setattr(main, "execute_artifact_query", deny)
    denied = client.post(
        "/artifacts/srp/pricing-insights/data",
        headers=_auth_headers(token),
        json={"operation": "details"},
    )
    assert denied.status_code == 403
    assert denied.json() == {"detail": "Artifact access denied"}


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
            "refresh_mode": "ttl",
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
        "refresh_mode": "ttl",
    }
    assert captured["queries"] == queries


def test_artifact_asset_route_is_authenticated_and_revalidated(monkeypatch):
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
    assert response.headers["cache-control"] == "private, no-cache"
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
