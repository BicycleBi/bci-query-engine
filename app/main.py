"""
main.py — FastAPI routes for the Query Engine.
"""
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from .engine import get_run, run_artifact
from .models import HealthResponse, RunMode, RunResponse

app = FastAPI(title="BCI Query Engine", version="0.1.0")


@app.get("/health", response_model=HealthResponse)
def health():
    return HealthResponse(status="ok")


@app.post("/run/{client_key}/{artifact_key}", response_model=RunResponse, status_code=202)
def trigger_run(
    client_key: str,
    artifact_key: str,
    mode: RunMode = RunMode.email,
):
    """
    Trigger an artifact run.

    mode=email    — render + send email (respects delivery_mode)
    mode=preview  — render only, return HTML in preview_html field, no log
    mode=dry-run  — render + log, no send
    """
    result = run_artifact(client_key, artifact_key, mode=mode.value)

    if result.get("status") == "error":
        raise HTTPException(status_code=500, detail=result.get("error_message"))

    return RunResponse(**result)


@app.get("/run/{run_id}", response_model=RunResponse)
def get_run_status(run_id: str):
    """Fetch the status and metadata for a previous run."""
    record = get_run(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    return RunResponse(**record)
