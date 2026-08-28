"""
main.py — FastAPI routes for the Query Engine.
"""
import base64
import hashlib
import hmac
import json
import os
import time
import threading
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Header, Query
from fastapi.responses import HTMLResponse, Response

from .engine import (
    execute_artifact,
    execute_artifact_query,
    execute_queued_artifact,
    get_artifact_asset,
    get_latest_artifact_deliveries,
    get_run,
    create_delivery_batch,
    get_delivery_batch,
    retry_delivery_batch_item,
    reconcile_delivery_batch,
    run_delivery_worker,
    prewarm_artifact_query_cache,
    queue_artifact_execution,
    write_artifact_definition,
)
from .models import (
    ArtifactExecutionRequest,
    ArtifactExecutionResponse,
    ArtifactDeliveryHistoryItem,
    ArtifactQueryCachePrewarmRequest,
    ArtifactQueryCachePrewarmResponse,
    ArtifactWriteRequest,
    ArtifactWriteResponse,
    HealthResponse,
    DeliveryBatchRequest,
    DeliveryBatchResponse,
    DeliveryRetryRequest,
    RunMode,
    RunResponse,
)

_delivery_worker_started = False


def start_delivery_worker() -> None:
    global _delivery_worker_started
    if _delivery_worker_started or os.getenv("ARTIFACT_DELIVERY_WORKER_ENABLED", "true").lower() != "true":
        return
    try:
        worker_count = max(1, int(os.getenv("ARTIFACT_DELIVERY_WORKERS", "2")))
    except ValueError:
        worker_count = 2
    for worker_index in range(worker_count):
        thread = threading.Thread(
            target=run_delivery_worker,
            name=f"artifact-delivery-worker-{worker_index + 1}",
            daemon=True,
        )
        thread.start()
    _delivery_worker_started = True


@asynccontextmanager
async def lifespan(_app: FastAPI):
    start_delivery_worker()
    yield


app = FastAPI(title="BCI Query Engine", version="0.1.0", lifespan=lifespan)
SECURITY_TOKEN_SECRET = os.getenv("QUERY_ENGINE_SECURITY_TOKEN_SECRET", os.getenv("SECURITY_TOKEN_SECRET", "dev-only-change-me"))
SECURITY_TOKEN_ISSUER = os.getenv("QUERY_ENGINE_SECURITY_TOKEN_ISSUER", os.getenv("SECURITY_TOKEN_ISSUER", "bci-security"))
SECURITY_TOKEN_AUDIENCE = os.getenv("QUERY_ENGINE_SECURITY_TOKEN_AUDIENCE", os.getenv("SECURITY_TOKEN_AUDIENCE", "bci-client"))
SERVICE_TOKEN = os.getenv("SERVICE_TOKEN", "")


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


def require_service_identity(authorization: Optional[str] = Header(default=None)) -> None:
    if not SERVICE_TOKEN:
        raise HTTPException(status_code=503, detail="Internal service authentication is unavailable")
    supplied = authorization.removeprefix("Bearer ").strip() if authorization and authorization.startswith("Bearer ") else ""
    if not supplied or not hmac.compare_digest(supplied, SERVICE_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid internal service token")


def require_client_access(identity: dict[str, Any], client_key: str) -> None:
    token_client_key = identity.get("client_key")
    if not token_client_key:
        raise HTTPException(status_code=403, detail="Internal token is missing client scope")
    if token_client_key != client_key:
        raise HTTPException(status_code=403, detail="Client access denied")


def authenticated_subject(identity: dict[str, Any]) -> str:
    subject = identity.get("sub")
    if not isinstance(subject, str) or not subject.strip():
        raise HTTPException(status_code=403, detail="Internal token is missing subject identity")
    return subject.strip()


def authorized_roles(
    identity: dict[str, Any],
    forwarded_roles: Optional[str] = None,
) -> list[str]:
    roles: Any
    if forwarded_roles is not None and forwarded_roles.strip():
        roles = [role.strip() for role in forwarded_roles.split(",") if role.strip()]
    else:
        roles = identity.get("roles", [])
    if not isinstance(roles, list):
        raise HTTPException(status_code=403, detail="Internal token has invalid authorization roles")
    if any(not isinstance(role, str) or not role.strip() for role in roles):
        raise HTTPException(status_code=403, detail="Internal token has invalid authorization roles")
    normalized_roles = sorted(set(role.strip() for role in roles))
    if not normalized_roles:
        raise HTTPException(status_code=403, detail="Identity has no authorization roles")
    return normalized_roles


def require_delivery_control(roles: list[str]) -> None:
    configured = {
        role.strip()
        for role in os.getenv("ARTIFACT_DELIVERY_CONTROL_ROLES", "").split(",")
        if role.strip()
    }
    if not configured:
        raise HTTPException(status_code=503, detail="Delivery control authorization is not configured")
    if configured.isdisjoint(roles):
        raise HTTPException(status_code=403, detail="Delivery control access denied")


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
    x_identity_roles: Optional[str] = Header(default=None),
    identity: dict[str, Any] = Depends(require_internal_identity),
):
    """Render and return the artifact HTML for display retrieval."""
    require_client_access(identity, client_key)
    result = execute_artifact(
        client_key,
        artifact_key,
        behavior="display",
        refresh_cache=refresh,
        authenticated_subject=authenticated_subject(identity),
        authorized_roles=authorized_roles(identity, x_identity_roles),
    )

    if result.get("status") == "error":
        raise HTTPException(status_code=500, detail=result.get("error_message"))

    html = result.get("preview_html")
    if html is None:
        raise HTTPException(status_code=500, detail="Artifact display returned no HTML")

    headers = _cache_headers(result.get("cache") or {})
    return HTMLResponse(content=html, headers=headers)


@app.post("/artifacts/{client_key}/{artifact_key}/data")
def get_artifact_data(
    client_key: str,
    artifact_key: str,
    query: dict[str, Any],
    x_identity_roles: Optional[str] = Header(default=None),
    identity: dict[str, Any] = Depends(require_internal_identity),
):
    """Pass an opaque artifact request to its database-owned query contract."""
    require_client_access(identity, client_key)
    try:
        return execute_artifact_query(
            client_key,
            artifact_key,
            query=query,
            authenticated_subject=authenticated_subject(identity),
            authorized_roles=authorized_roles(identity, x_identity_roles),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(
    "/internal/artifacts/{client_key}/{artifact_key}/query-cache/prewarm",
    response_model=ArtifactQueryCachePrewarmResponse,
)
def prewarm_artifact_data_cache(
    client_key: str,
    artifact_key: str,
    request: ArtifactQueryCachePrewarmRequest,
    _service_identity: None = Depends(require_service_identity),
):
    """Rebuild explicitly shared artifact-query Redis entries after a data load."""
    try:
        result = prewarm_artifact_query_cache(
            client_key,
            artifact_key,
            queries=request.queries,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ArtifactQueryCachePrewarmResponse(**result)


@app.get("/artifacts/{client_key}/{artifact_key}/assets/{asset_path:path}")
def get_artifact_static_asset(
    client_key: str,
    artifact_key: str,
    asset_path: str,
    identity: dict[str, Any] = Depends(require_internal_identity),
):
    """Return an authenticated, immutable package-owned artifact asset."""
    require_client_access(identity, client_key)
    try:
        asset = get_artifact_asset(client_key, artifact_key, asset_path)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return Response(
        content=asset["content"],
        media_type=asset["content_type"],
        headers={
            "Cache-Control": "private, max-age=31536000, immutable",
            "ETag": f'"{asset["sha256"]}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


def _cache_headers(cache: dict) -> dict[str, str]:
    headers: dict[str, str] = {}
    if not cache:
        return headers

    headers["X-BCI-Cache"] = str(cache.get("status", "unknown"))
    headers["X-BCI-Cache-Enabled"] = str(bool(cache.get("enabled", False))).lower()

    for source, header in (
        ("row_count", "X-BCI-Cache-Row-Count"),
        ("data_freshness_timestamp", "X-BCI-Data-Freshness"),
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
def create_artifact_execution(
    request: ArtifactExecutionRequest,
    x_identity_roles: Optional[str] = Header(default=None),
    identity: dict[str, Any] = Depends(require_internal_identity),
):
    """Create an execution request for an artifact."""
    require_client_access(identity, request.client_key)
    subject = authenticated_subject(identity)
    roles = authorized_roles(identity, x_identity_roles)

    if request.behavior.value == "deliver":
        try:
            result = queue_artifact_execution(
                request.client_key,
                request.artifact_key,
                authenticated_subject=subject,
                authorized_roles=roles,
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return ArtifactExecutionResponse(**result)

    result = execute_artifact(
        request.client_key,
        request.artifact_key,
        behavior=request.behavior.value,
        output_formats=[output_format.value for output_format in request.output_formats],
        authenticated_subject=subject,
        authorized_roles=roles,
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
    require_client_access(identity, record["client_key"])
    return ArtifactExecutionResponse(**record)


@app.get("/artifact-deliveries/latest", response_model=list[ArtifactDeliveryHistoryItem])
def get_latest_artifact_delivery_statuses(
    client_key: str,
    artifact_key: list[str] = Query(),
    identity: dict[str, Any] = Depends(require_internal_identity),
):
    """Return latest accepted delivery times without exposing recipients or message content."""
    require_client_access(identity, client_key)
    artifact_keys = list(dict.fromkeys(value.strip() for value in artifact_key if value.strip()))
    if not artifact_keys or len(artifact_keys) > 100:
        raise HTTPException(status_code=400, detail="Provide between 1 and 100 artifact keys")
    return [
        ArtifactDeliveryHistoryItem(**item)
        for item in get_latest_artifact_deliveries(client_key, artifact_keys)
    ]


@app.post("/delivery-batches", response_model=DeliveryBatchResponse, status_code=202)
def create_artifact_delivery_batch(
    request: DeliveryBatchRequest,
    x_identity_roles: Optional[str] = Header(default=None),
    identity: dict[str, Any] = Depends(require_internal_identity),
):
    require_client_access(identity, request.client_key)
    roles = authorized_roles(identity, x_identity_roles)
    require_delivery_control(roles)
    try:
        result = create_delivery_batch(
            request.client_key,
            request.artifact_keys,
            reporting_period=request.reporting_period,
            idempotency_key=request.idempotency_key,
            mode=request.mode.value,
            authenticated_subject=authenticated_subject(identity),
            authorized_roles=roles,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return DeliveryBatchResponse(**result)


@app.get("/delivery-batches/{batch_id}", response_model=DeliveryBatchResponse)
def get_artifact_delivery_batch(
    batch_id: str,
    x_identity_roles: Optional[str] = Header(default=None),
    identity: dict[str, Any] = Depends(require_internal_identity),
):
    roles = authorized_roles(identity, x_identity_roles)
    require_delivery_control(roles)
    result = get_delivery_batch(batch_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Delivery batch not found")
    require_client_access(identity, result["client_key"])
    return DeliveryBatchResponse(**result)


@app.post(
    "/delivery-batches/{batch_id}/items/{item_id}/retry",
    response_model=DeliveryBatchResponse,
    status_code=202,
)
def retry_artifact_delivery_batch_item(
    batch_id: str,
    item_id: str,
    request: DeliveryRetryRequest,
    x_identity_roles: Optional[str] = Header(default=None),
    identity: dict[str, Any] = Depends(require_internal_identity),
):
    roles = authorized_roles(identity, x_identity_roles)
    require_delivery_control(roles)
    batch = get_delivery_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Delivery batch not found")
    require_client_access(identity, batch["client_key"])
    try:
        result = retry_delivery_batch_item(
            batch_id,
            item_id,
            reason=request.reason,
            authenticated_subject=authenticated_subject(identity),
            authorized_roles=roles,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return DeliveryBatchResponse(**result)


@app.post(
    "/delivery-batches/{batch_id}/reconcile",
    response_model=DeliveryBatchResponse,
)
def reconcile_artifact_delivery_batch(
    batch_id: str,
    x_identity_roles: Optional[str] = Header(default=None),
    identity: dict[str, Any] = Depends(require_internal_identity),
):
    roles = authorized_roles(identity, x_identity_roles)
    require_delivery_control(roles)
    batch = get_delivery_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Delivery batch not found")
    require_client_access(identity, batch["client_key"])
    return DeliveryBatchResponse(**reconcile_delivery_batch(batch_id))


@app.post("/run/{client_key}/{artifact_key}", response_model=RunResponse, status_code=202, deprecated=True)
def trigger_run(
    client_key: str,
    artifact_key: str,
    mode: RunMode = RunMode.email,
    x_identity_roles: Optional[str] = Header(default=None),
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
    result = execute_artifact(
        client_key,
        artifact_key,
        behavior=legacy_behavior[mode],
        authenticated_subject=authenticated_subject(identity),
        authorized_roles=authorized_roles(identity, x_identity_roles),
    )

    if result.get("status") == "error":
        raise HTTPException(status_code=500, detail=result.get("error_message"))

    return RunResponse(**result)


@app.get("/run/{run_id}", response_model=RunResponse, deprecated=True)
def get_run_status(run_id: str, identity: dict[str, Any] = Depends(require_internal_identity)):
    """Legacy alias for artifact execution status."""
    record = get_run(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    require_client_access(identity, record["client_key"])
    return RunResponse(**record)
