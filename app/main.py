"""
main.py — FastAPI routes for the Query Engine.
"""
import base64
import hashlib
import hmac
import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Header, Request
from fastapi.responses import HTMLResponse, Response

from .engine import (
    execute_artifact,
    execute_artifact_query,
    execute_queued_artifact,
    get_artifact_asset,
    get_artifact_distribution_groups,
    get_run,
    prewarm_artifact_query_cache,
    queue_artifact_execution,
    write_artifact_definition,
)
from .models import (
    ArtifactExecutionRequest,
    ArtifactExecutionResponse,
    ArtifactDistributionGroupsResponse,
    ArtifactQueryCachePrewarmRequest,
    ArtifactQueryCachePrewarmResponse,
    ArtifactWriteRequest,
    ArtifactWriteResponse,
    HealthResponse,
    RunMode,
    RunResponse,
    UsageInteractionRequest,
    UsageInteractionResponse,
    UsageEventIngestRequest,
    UsageEventIngestResponse,
)
from .monitoring import (
    record_ingested_event_async,
    record_interaction_event_async,
    record_request_span_async,
    start_gateway_listener,
    stop_gateway_listener,
)

@asynccontextmanager
async def lifespan(_app: FastAPI):
    start_gateway_listener()
    try:
        yield
    finally:
        stop_gateway_listener()


app = FastAPI(title="BCI Query Engine", version="0.2.0", lifespan=lifespan)
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


def require_internal_identity(
    request: Request,
    authorization: Optional[str] = Header(default=None),
) -> dict[str, Any]:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing internal authorization token")
    token = authorization.removeprefix("Bearer ").strip()
    identity = _verify_internal_token(token)
    request.state.monitoring_identity = identity
    return identity


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


def _safe_request_id(value: Optional[str]) -> str:
    candidate = (value or "").strip()
    if candidate and len(candidate) <= 128 and all(
        character.isalnum() or character in "-_." for character in candidate
    ):
        return candidate
    return uuid.uuid4().hex


def _request_scope(path: str) -> tuple[Optional[str], Optional[str]]:
    parts = [part for part in path.split("/") if part]
    if len(parts) >= 3 and parts[0] == "artifacts":
        return parts[1], parts[2]
    if len(parts) >= 3 and parts[0] == "run":
        return parts[1], parts[2]
    if len(parts) >= 2 and parts[0] == "artifact-executions":
        return None, None
    if len(parts) >= 3 and parts[0] == "usage" and parts[1] == "interactions":
        return parts[2], parts[3] if len(parts) >= 4 else None
    return None, None


@app.middleware("http")
async def monitor_request_lifecycle(request: Request, call_next):
    started_at = datetime.now(tz=timezone.utc)
    started_perf = time.perf_counter()
    request_id = _safe_request_id(request.headers.get("X-Request-ID"))
    request.state.monitoring_request_id = request_id
    response_status = 500
    response = None
    try:
        response = await call_next(request)
        response_status = response.status_code
        return response
    finally:
        completed_at = datetime.now(tz=timezone.utc)
        route = request.scope.get("route")
        route_template = getattr(route, "path", None) or "unmatched"
        client_key, artifact_key = _request_scope(request.url.path)
        client_key = getattr(request.state, "monitoring_client_key", None) or client_key
        artifact_key = getattr(request.state, "monitoring_artifact_key", None) or artifact_key
        identity = getattr(request.state, "monitoring_identity", None)
        if identity and not client_key:
            client_key = str(identity.get("client_key") or "").strip() or None
        headers = response.headers if response is not None else {}

        def _numeric_header(name: str) -> Optional[float]:
            try:
                value = headers.get(name)
                return float(value) if value is not None else None
            except (TypeError, ValueError):
                return None

        if request.url.path not in {"/health", "/internal/usage/events"}:
            record_request_span_async(
                request_id=request_id,
                identity=identity,
                client_key=client_key,
                method=request.method.upper(),
                route_template=route_template,
                artifact_key=artifact_key,
                run_id=getattr(request.state, "monitoring_run_id", None),
                started_at=started_at,
                completed_at=completed_at,
                duration_ms=max(0, int((time.perf_counter() - started_perf) * 1000)),
                response_status=response_status,
                reason_code=(
                    "authentication_denied" if response_status in {401, 403} else
                    "request_failed" if response_status >= 400 else None
                ),
                database_ms=_numeric_header("X-BCI-Data-Query-Ms"),
                render_ms=_numeric_header("X-BCI-Render-Ms"),
                cache_status=headers.get("X-BCI-Cache"),
            )
        if response is not None:
            response.headers["X-Request-ID"] = request_id


@app.get("/health", response_model=HealthResponse)
def health():
    return HealthResponse(status="ok")


@app.post(
    "/internal/usage/events",
    response_model=UsageEventIngestResponse,
    status_code=202,
    dependencies=[Depends(require_service_identity)],
)
def ingest_usage_event(event: UsageEventIngestRequest) -> UsageEventIngestResponse:
    """Accept only bounded metadata from trusted stack services."""
    record_ingested_event_async(**event.model_dump())
    return UsageEventIngestResponse()


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
    request: Request,
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
    request.state.monitoring_run_id = result.get("run_id")

    if result.get("status") == "error":
        raise HTTPException(status_code=500, detail=result.get("error_message"))

    html = result.get("preview_html")
    if html is None:
        raise HTTPException(status_code=500, detail="Artifact display returned no HTML")

    headers = _cache_headers(result.get("cache") or {})
    return HTMLResponse(content=html, headers=headers)


@app.post(
    "/usage/interactions/{client_key}/{artifact_key}",
    response_model=UsageInteractionResponse,
    status_code=202,
)
def record_usage_interaction(
    client_key: str,
    artifact_key: str,
    interaction: UsageInteractionRequest,
    request: Request,
    identity: dict[str, Any] = Depends(require_internal_identity),
):
    """Accept a payload-free, allowlisted dashboard interaction event."""
    require_client_access(identity, client_key)
    if interaction.client_key != client_key or interaction.artifact_key != artifact_key:
        raise HTTPException(status_code=400, detail="Interaction route and body do not match")
    record_interaction_event_async(
        identity=identity,
        client_key=client_key,
        artifact_key=artifact_key,
        event_type=interaction.interaction_type,
        event_key=interaction.interaction_key,
        event_status=interaction.status,
        request_id=getattr(request.state, "monitoring_request_id", None),
        duration_ms=interaction.duration_ms,
        reason_code=interaction.reason_code,
    )
    return UsageInteractionResponse()


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


@app.get(
    "/artifacts/{client_key}/{artifact_key}/distribution-groups",
    response_model=ArtifactDistributionGroupsResponse,
)
def list_artifact_distribution_groups(
    client_key: str,
    artifact_key: str,
    identity: dict[str, Any] = Depends(require_internal_identity),
):
    """List active groups approved for an artifact without recipient details."""
    require_client_access(identity, client_key)
    try:
        groups = get_artifact_distribution_groups(client_key, artifact_key)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ArtifactDistributionGroupsResponse(
        client_key=client_key,
        artifact_key=artifact_key,
        groups=groups,
    )


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
    execution: ArtifactExecutionRequest,
    http_request: Request,
    background_tasks: BackgroundTasks,
    x_identity_roles: Optional[str] = Header(default=None),
    identity: dict[str, Any] = Depends(require_internal_identity),
):
    """Create an execution request for an artifact."""
    require_client_access(identity, execution.client_key)
    http_request.state.monitoring_client_key = execution.client_key
    http_request.state.monitoring_artifact_key = execution.artifact_key
    subject = authenticated_subject(identity)
    roles = authorized_roles(identity, x_identity_roles)

    if execution.behavior.value == "deliver":
        try:
            result = queue_artifact_execution(execution.client_key, execution.artifact_key)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        http_request.state.monitoring_run_id = result.get("run_id")
        background_tasks.add_task(
            execute_queued_artifact,
            execution.client_key,
            execution.artifact_key,
            behavior=execution.behavior.value,
            output_formats=[output_format.value for output_format in execution.output_formats],
            execution_query=execution.query,
            distribution_group_keys=execution.distribution_group_keys,
            authenticated_subject=subject,
            authorized_roles=roles,
            run_id=result["run_id"],
            started_at=result["started_at"],
            precreated_run=True,
        )
        return ArtifactExecutionResponse(**result)

    result = execute_artifact(
        execution.client_key,
        execution.artifact_key,
        behavior=execution.behavior.value,
        output_formats=[output_format.value for output_format in execution.output_formats],
        execution_query=execution.query,
        distribution_group_keys=execution.distribution_group_keys,
        authenticated_subject=subject,
        authorized_roles=roles,
    )
    http_request.state.monitoring_run_id = result.get("run_id")

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
