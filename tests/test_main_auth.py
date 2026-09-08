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
        execution_query,
        distribution_group_keys,
        authenticated_subject,
        authorized_roles,
    ):
        captured["authenticated_subject"] = authenticated_subject
        captured["authorized_roles"] = authorized_roles
        captured["output_formats"] = output_formats
        captured["execution_query"] = execution_query
        captured["distribution_group_keys"] = distribution_group_keys
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


def test_distribution_group_route_returns_approved_group_labels(monkeypatch):
    main = _load_main(monkeypatch)
    client = TestClient(main.app)
    monkeypatch.setattr(
        main,
        "get_artifact_distribution_groups",
        lambda client_key, artifact_key: [
            {
                "group_key": "operations",
                "display_name": "Operations",
                "description": "Operations audience",
            }
        ],
    )
    token = _encode_token(
        {
            "aud": "bci-client",
            "client_key": "rf",
            "exp": int(time.time()) + 3600,
            "iat": int(time.time()),
            "iss": "bci-security",
            "roles": ["developer"],
            "sub": "user-1",
        }
    )

    response = client.get(
        "/artifacts/rf/market-penetration-fdh-csv-burst/distribution-groups",
        headers=_auth_headers(token),
    )

    assert response.status_code == 200
    assert response.json() == {
        "client_key": "rf",
        "artifact_key": "market-penetration-fdh-csv-burst",
        "groups": [
            {
                "group_key": "operations",
                "display_name": "Operations",
                "description": "Operations audience",
            }
        ],
    }


def test_delivery_execution_returns_durable_queued_run(monkeypatch):
    main = _load_main(monkeypatch)
    client = TestClient(main.app)
    captured = {}
    background = {}
    started_at = datetime.now(tz=timezone.utc)

    monkeypatch.setattr(
        main,
        "queue_artifact_execution",
        lambda client_key, artifact_key, **kwargs: captured.update(kwargs) or {
            "run_id": "11111111-1111-1111-1111-111111111111",
            "client_key": client_key,
            "artifact_key": artifact_key,
            "status": "queued",
            "started_at": started_at,
            "completed_at": None,
            "outputs": [],
        },
    )
    monkeypatch.setattr(
        main,
        "execute_queued_artifact",
        lambda client_key, artifact_key, **kwargs: background.update(
            client_key=client_key,
            artifact_key=artifact_key,
            **kwargs,
        ),
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
            "output_formats": ["csv"],
            "query": {"action": "hierarchy-export", "statuses": ["21_Area Live"]},
            "distribution_group_keys": ["test-reviewers"],
        },
    )

    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    assert response.json()["run_id"] == "11111111-1111-1111-1111-111111111111"
    assert captured["authenticated_subject"] == "user-1"
    assert captured["authorized_roles"] == ["srp_pnl_scope_a"]
    assert captured["execution_mode"] == "background"
    assert background["output_formats"] == ["csv"]
    assert background["execution_query"] == {
        "action": "hierarchy-export",
        "statuses": ["21_Area Live"],
    }
    assert background["distribution_group_keys"] == ["test-reviewers"]


def test_simple_delivery_execution_is_left_for_durable_worker(monkeypatch):
    main = _load_main(monkeypatch)
    client = TestClient(main.app)
    captured = {}
    started_at = datetime.now(tz=timezone.utc)
    monkeypatch.setattr(
        main,
        "queue_artifact_execution",
        lambda client_key, artifact_key, **kwargs: captured.update(kwargs) or {
            "run_id": "11111111-1111-1111-1111-111111111111",
            "client_key": client_key,
            "artifact_key": artifact_key,
            "status": "queued",
            "started_at": started_at,
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

    response = client.post(
        "/artifact-executions",
        headers={**_auth_headers(token), "X-Identity-Roles": "srp_pnl_scope_a"},
        json={
            "client_key": "srp",
            "artifact_key": "visit-counts",
            "behavior": "deliver",
        },
    )

    assert response.status_code == 202
    assert captured["execution_mode"] == "live"


def test_queue_cancel_requires_control_role_and_returns_count(monkeypatch):
    main = _load_main(monkeypatch)
    client = TestClient(main.app)
    monkeypatch.setenv("ARTIFACT_DELIVERY_CONTROL_ROLES", "srp_production_admin")
    monkeypatch.setattr(
        main,
        "cancel_queued_artifact_executions",
        lambda client_key, run_ids, reason: {
            "status": "cancelled",
            "cancelled_count": len(run_ids),
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
    payload = {
        "client_key": "srp",
        "run_ids": ["11111111-1111-1111-1111-111111111111"],
        "reason": "Verified synchronous recovery delivered this report",
    }

    denied = client.post(
        "/artifact-delivery-queue/cancel",
        headers=_auth_headers(token),
        json=payload,
    )
    accepted = client.post(
        "/artifact-delivery-queue/cancel",
        headers={**_auth_headers(token), "X-Identity-Roles": "srp_production_admin"},
        json=payload,
    )

    assert denied.status_code == 403
    assert accepted.status_code == 200
    assert accepted.json() == {"status": "cancelled", "cancelled_count": 1}


def test_delivery_batch_requires_configured_control_role(monkeypatch):
    main = _load_main(monkeypatch)
    client = TestClient(main.app)
    monkeypatch.setenv("ARTIFACT_DELIVERY_CONTROL_ROLES", "srpdev_pnl_delivery_operator")
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
        "/delivery-batches",
        headers={**_auth_headers(token), "X-Identity-Roles": "srp_pnl_scope_a"},
        json={
            "client_key": "srp",
            "artifact_keys": ["pnl-owner-email-owner-1"],
            "reporting_period": "July 2026",
            "idempotency_key": "july-2026-owner-batch",
            "mode": "live",
        },
    )

    assert response.status_code == 403


def test_delivery_batch_passes_server_resolved_identity_context(monkeypatch):
    main = _load_main(monkeypatch)
    client = TestClient(main.app)
    captured = {}
    created_at = datetime.now(tz=timezone.utc)
    monkeypatch.setenv("ARTIFACT_DELIVERY_CONTROL_ROLES", "srpdev_pnl_delivery_operator")

    def fake_create_delivery_batch(client_key, artifact_keys, **kwargs):
        captured.update(client_key=client_key, artifact_keys=artifact_keys, **kwargs)
        return {
            "batch_id": "11111111-1111-1111-1111-111111111111",
            "client_key": client_key,
            "reporting_period": kwargs["reporting_period"],
            "mode": kwargs["mode"],
            "status": "in_progress",
            "created_at": created_at,
            "items": [
                {
                    "item_id": "22222222-2222-2222-2222-222222222222",
                    "artifact_key": artifact_keys[0],
                    "run_id": "33333333-3333-3333-3333-333333333333",
                    "status": "queued",
                    "attempt_number": 1,
                }
            ],
        }

    monkeypatch.setattr(main, "create_delivery_batch", fake_create_delivery_batch)
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
        "/delivery-batches",
        headers={
            **_auth_headers(token),
            "X-Identity-Roles": "srpdev_pnl_delivery_operator,srp_pnl_scope_a",
        },
        json={
            "client_key": "srp",
            "artifact_keys": ["pnl-owner-email-owner-1"],
            "reporting_period": "July 2026",
            "idempotency_key": "july-2026-owner-batch",
            "mode": "internal_test",
        },
    )

    assert response.status_code == 202
    assert captured["authenticated_subject"] == "user-1"
    assert captured["authorized_roles"] == ["srp_pnl_scope_a", "srpdev_pnl_delivery_operator"]
    assert captured["mode"] == "internal_test"


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


def test_execution_delivery_reconcile_returns_exchange_status(monkeypatch):
    main = _load_main(monkeypatch)
    client = TestClient(main.app)
    run_id = "33333333-3333-3333-3333-333333333333"
    started_at = datetime.now(tz=timezone.utc)
    record = {
        "run_id": run_id,
        "client_key": "srp",
        "artifact_key": "pnl-owner-single-training-email",
        "status": "completed",
        "started_at": started_at,
        "completed_at": started_at,
        "delivery_status": "provider_accepted",
        "outputs": [],
    }
    monkeypatch.setattr(main, "get_run", lambda requested_run_id: record)
    monkeypatch.setattr(
        main,
        "reconcile_artifact_delivery",
        lambda requested_run_id: {**record, "delivery_status": "delivered"},
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

    response = client.post(
        f"/artifact-executions/{run_id}/reconcile",
        headers=_auth_headers(token),
    )

    assert response.status_code == 200
    assert response.json()["delivery_status"] == "delivered"


def test_latest_artifact_deliveries_returns_nonrecipient_history(monkeypatch):
    main = _load_main(monkeypatch)
    client = TestClient(main.app)
    sent_at = datetime(2026, 8, 28, 10, 6, 53, tzinfo=timezone.utc)
    captured = {}

    def fake_latest(client_key, artifact_keys):
        captured["client_key"] = client_key
        captured["artifact_keys"] = artifact_keys
        return [
            {
                "artifact_key": "pnl-owner-single-training-email",
                "run_id": UUID("44444444-4444-4444-4444-444444444444"),
                "delivery_status": "provider_accepted",
                "sent_at": sent_at,
                "reporting_period": None,
                "batch_id": UUID("55555555-5555-5555-5555-555555555555"),
                "item_id": UUID("66666666-6666-6666-6666-666666666666"),
            }
        ]

    monkeypatch.setattr(main, "get_latest_artifact_deliveries", fake_latest)
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
        "/artifact-deliveries/latest",
        params=[
            ("client_key", "srp"),
            ("artifact_key", "pnl-owner-single-training-email"),
        ],
        headers=_auth_headers(token),
    )

    assert response.status_code == 200
    assert captured == {
        "client_key": "srp",
        "artifact_keys": ["pnl-owner-single-training-email"],
    }
    assert response.json() == [
        {
            "artifact_key": "pnl-owner-single-training-email",
            "run_id": "44444444-4444-4444-4444-444444444444",
            "delivery_status": "provider_accepted",
            "sent_at": "2026-08-28T10:06:53Z",
            "reporting_period": None,
            "batch_id": "55555555-5555-5555-5555-555555555555",
            "item_id": "66666666-6666-6666-6666-666666666666",
        }
    ]


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
