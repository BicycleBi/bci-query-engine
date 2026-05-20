"""
models.py — Pydantic request/response models for the Query Engine API.
"""
from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel


class RunMode(str, Enum):
    email = "email"
    preview = "preview"
    dry_run = "dry-run"


class RunResponse(BaseModel):
    run_id: Optional[str] = None
    client_key: str
    artifact_key: str
    status: str
    started_at: datetime
    completed_at: Optional[datetime] = None
    error_message: Optional[str] = None
    preview_html: Optional[str] = None


class HealthResponse(BaseModel):
    status: str
    service: str = "bci-query-engine"
