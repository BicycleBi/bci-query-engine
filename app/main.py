"""
main.py — FastAPI routes for the Query Engine.
"""
import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Header
from fastapi.responses import HTMLResponse

from .engine import execute_artifact, get_run, write_artifact_definition
from .models import (
    ArtifactExecutionRequest,
    ArtifactExecutionResponse,
    ArtifactWriteRequest,
    ArtifactWriteResponse,
    HealthResponse,
    RunMode,
    RunResponse,
)

app = FastAPI(title="BCI Query Engine", version="0.1.0")
SECURITY_TOKEN_SECRET = os.getenv("QUERY_ENGINE_SECURITY_TOKEN_SECRET", os.getenv("SECURITY_TOKEN_SECRET", "dev-only-change-me"))
SECURITY_TOKEN_ISSUER = os.getenv("QUERY_ENGINE_SECURITY_TOKEN_ISSUER", os.getenv("SECURITY_TOKEN_ISSUER", "bci-security"))
SECURITY_TOKEN_AUDIENCE = os.getenv("QUERY_ENGINE_SECURITY_TOKEN_AUDIENCE", os.getenv("SECURITY_TOKEN_AUDIENCE", "bci-client"))


def _token_signature(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    signature = hmac.new(SECURITY_TOKEN_SECRET.encode("utf-8"), raw, hashlib.sha256).digest()
    return (
        base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")
        + "."
        + base64.urlsafe_b64encode(signature).decode("utf-8").rstrip("=")
    )


def _verify_internal_token(token: str) -> dict[str, Any]:
    try:
        payload_part, signature_part = token.split(".", 1)
        raw = base64.urlsafe_b64decode(payload_part + "=" * (-len(payload_part) % 4))
        signature = base64.urlsafe_b64decode(signature_part + "=" * (-len(signature_part) % 4))
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid internal token format") from exc

    expected = hmac.new(SECURITY_TOKEN_SECRET.encode("utf-8"), raw, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=401, detail="Invalid internal token signature")

    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid internal token payload") from exc

    if payload.get("iss") != SECURITY_TOKEN_ISSUER:
        raise HTTPException(status_code=401, detail="Invalid internal token issuer")
    if payload.get("aud") != SECURITY_TOKEN_AUDIENCE:
        raise HTTPException(status_code=401, detail="Invalid internal token audience")

    exp = payload.get("exp")
    if not isinstance(exp, int) or exp <= int(time.time()):
        raise HTTPException(status_code=401, detail="Internal token expired")

    return payload


def require_internal_identity(authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing internal authorization token")
    token = authorization.removeprefix("Bearer ").strip()
    return _verify_internal_token(token)


def require_client_access(identity: dict[str, Any], client_key: str) -> None:
    token_client_key = identity.get("client_key")
    if not token_client_key:
        raise HTTPException(status_code=403, detail="Internal token is missing client scope")
    if token_client_key != client_key:
        raise HTTPException(status_code=403, detail="Client access denied")


@app.get("/health", response_model=HealthResponse)
def health():
    return HealthResponse(status="ok")


@app.post("/artifacts", response_model=ArtifactWriteResponse, status_code=201)
def save_artifact(definition: ArtifactWriteRequest, identity: dict[str, Any] = Depends(require_internal_identity)):
    """Create or update an artifact definition in metadata."""
    require_client_access(identity, definition.client_key)
    result = write_artifact_definition(definition.model_dump())
    return ArtifactWriteResponse(**result)


@app.get("/artifacts/{client_key}/{artifact_key}", response_class=HTMLResponse)
def get_artifact_html(
    client_key: str,
    artifact_key: str,
    refresh: bool = False,
    identity: dict[str, Any] = Depends(require_internal_identity),
):
    """Render and return the artifact HTML for display retrieval."""
    require_client_access(identity, client_key)
    result = execute_artifact(
        client_key,
        artifact_key,
        behavior="display",
        refresh_cache=refresh,
    )

    if result.get("status") == "error":
        raise HTTPException(status_code=500, detail=result.get("error_message"))

    html = result.get("preview_html")
    if html is None:
        raise HTTPException(status_code=500, detail="Artifact display returned no HTML")

    headers = _cache_headers(result.get("cache") or {})
    return HTMLResponse(content=html, headers=headers)


def _cache_headers(cache: dict) -> dict[str, str]:
    headers: dict[str, str] = {}
    if not cache:
        return headers

    headers["X-BCI-Cache"] = str(cache.get("status", "unknown"))
    headers["X-BCI-Cache-Enabled"] = str(bool(cache.get("enabled", False))).lower()

    for source, header in (
        ("row_count", "X-BCI-Cache-Row-Count"),
        ("cache_read_ms", "X-BCI-Cache-Read-Ms"),
        ("data_query_ms", "X-BCI-Data-Query-Ms"),
        ("render_ms", "X-BCI-Render-Ms"),
        ("total_ms", "X-BCI-Total-Ms"),
    ):
        value = cache.get(source)
        if value is None:
            continue
        if isinstance(value, float):
            headers[header] = f"{value:.3f}"
        else:
            headers[header] = str(value)

    return headers


@app.post("/artifact-executions", response_model=ArtifactExecutionResponse, status_code=202)
def create_artifact_execution(request: ArtifactExecutionRequest, identity: dict[str, Any] = Depends(require_internal_identity)):
    """Create an execution request for an artifact."""
    require_client_access(identity, request.client_key)
    result = execute_artifact(
        request.client_key,
        request.artifact_key,
        behavior=request.behavior.value,
    )

    if result.get("status") == "error":
        raise HTTPException(status_code=500, detail=result.get("error_message"))

    return ArtifactExecutionResponse(**result)


@app.get("/artifact-executions/{run_id}", response_model=ArtifactExecutionResponse)
def get_artifact_execution_status(run_id: str, identity: dict[str, Any] = Depends(require_internal_identity)):
    """Fetch the status and metadata for a previous artifact execution."""
    record = get_run(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    return ArtifactExecutionResponse(**record)


@app.post("/run/{client_key}/{artifact_key}", response_model=RunResponse, status_code=202, deprecated=True)
def trigger_run(
    client_key: str,
    artifact_key: str,
    mode: RunMode = RunMode.email,
    identity: dict[str, Any] = Depends(require_internal_identity),
):
    """
    Legacy alias for artifact execution.

    mode=email    — render + send email (respects delivery_mode)
    mode=preview  — render only, return HTML in preview_html field, no log
    mode=dry-run  — render + log, no send
    """
    legacy_behavior = {
        RunMode.email: "deliver",
        RunMode.preview: "preview",
        RunMode.dry_run: "dry-run",
    }
    require_client_access(identity, client_key)
    result = execute_artifact(client_key, artifact_key, behavior=legacy_behavior[mode])

    if result.get("status") == "error":
        raise HTTPException(status_code=500, detail=result.get("error_message"))

    return RunResponse(**result)


@app.get("/run/{run_id}", response_model=RunResponse, deprecated=True)
def get_run_status(run_id: str, identity: dict[str, Any] = Depends(require_internal_identity)):
    """Legacy alias for artifact execution status."""
    record = get_run(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    return RunResponse(**record)
