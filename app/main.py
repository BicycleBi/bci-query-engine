"""
main.py — FastAPI routes for the Query Engine.
"""
from fastapi import FastAPI, HTTPException
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


@app.get("/health", response_model=HealthResponse)
def health():
    return HealthResponse(status="ok")


@app.post("/artifacts", response_model=ArtifactWriteResponse, status_code=201)
def save_artifact(definition: ArtifactWriteRequest):
    """Create or update an artifact definition in metadata."""
    result = write_artifact_definition(definition.model_dump())
    return ArtifactWriteResponse(**result)


@app.get("/artifacts/{client_key}/{artifact_key}", response_class=HTMLResponse)
def get_artifact_html(client_key: str, artifact_key: str, refresh: bool = False):
    """Render and return the artifact HTML for display retrieval."""
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
def create_artifact_execution(request: ArtifactExecutionRequest):
    """Create an execution request for an artifact."""
    result = execute_artifact(
        request.client_key,
        request.artifact_key,
        behavior=request.behavior.value,
    )

    if result.get("status") == "error":
        raise HTTPException(status_code=500, detail=result.get("error_message"))

    return ArtifactExecutionResponse(**result)


@app.get("/artifact-executions/{run_id}", response_model=ArtifactExecutionResponse)
def get_artifact_execution_status(run_id: str):
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
    result = execute_artifact(client_key, artifact_key, behavior=legacy_behavior[mode])

    if result.get("status") == "error":
        raise HTTPException(status_code=500, detail=result.get("error_message"))

    return RunResponse(**result)


@app.get("/run/{run_id}", response_model=RunResponse, deprecated=True)
def get_run_status(run_id: str):
    """Legacy alias for artifact execution status."""
    record = get_run(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    return RunResponse(**record)
