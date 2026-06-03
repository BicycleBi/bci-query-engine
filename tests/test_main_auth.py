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
    monkeypatch.setattr(
        main,
        "execute_artifact",
        lambda client_key, artifact_key, behavior: {
            "run_id": "run-1",
            "status": "success",
            "client_key": client_key,
            "artifact_key": artifact_key,
            "started_at": datetime.now(tz=timezone.utc),
            "completed_at": datetime.now(tz=timezone.utc),
            "behavior": behavior,
            "preview_html": "<p>ok</p>",
        },
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
        headers=_auth_headers(token),
        json={
            "client_key": "srp",
            "artifact_key": "visit-counts",
            "behavior": "display",
        },
    )
    assert response.status_code == 202
    assert response.json()["preview_html"] == "<p>ok</p>"
