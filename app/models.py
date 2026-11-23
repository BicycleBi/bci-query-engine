from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class ArtifactExecutionBehavior(str, Enum):
    display = "display"
    dry_run = "dry-run"


class ArtifactOutputFormat(str, Enum):
    html = "html"
    pdf = "pdf"
    csv = "csv"


class ArtifactQueryCachePrewarmRequest(BaseModel):
    queries: list[dict[str, Any]] = Field(min_length=1, max_length=16)


class ArtifactQueryCachePrewarmResponse(BaseModel):
    client_key: str
    artifact_key: str
    status: str
    entry_count: int
    refresh_mode: Optional[str] = None


class ArtifactExecutionResponse(BaseModel):
    run_id: Optional[str] = None
    client_key: str
    artifact_key: str
    status: str
    started_at: datetime
    completed_at: Optional[datetime] = None
    error_message: Optional[str] = None
    preview_html: Optional[str] = None
    email_subject: Optional[str] = None
    outputs: list[dict] = Field(default_factory=list)

    @field_validator("run_id", mode="before")
    @classmethod
    def normalize_run_id(cls, value: Any) -> Optional[str]:
        return None if value is None else str(value)


class HealthResponse(BaseModel):
    status: str
    service: str = "bci-query-engine"


class UsageInteractionRequest(BaseModel):
    client_key: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    artifact_key: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    interaction_type: str = Field(
        pattern=r"^(dashboard_open|filter_apply|refresh|navigation|export_request|custom_action)$"
    )
    interaction_key: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    status: str = Field(default="completed", pattern=r"^(completed|succeeded|failed)$")
    duration_ms: Optional[int] = Field(default=None, ge=0, le=86_400_000)
    reason_code: Optional[str] = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,79}$")


class UsageInteractionResponse(BaseModel):
    status: str = "accepted"


class UsageEventIngestRequest(BaseModel):
    model_config = {"extra": "forbid"}

    source_service: str = Field(pattern=r"^(security|nginx)$")
    event_type: str = Field(pattern=r"^[a-z][a-z0-9_]{0,79}$")
    event_status: str = Field(pattern=r"^(started|completed|succeeded|failed|denied)$")
    client_key: Optional[str] = Field(default=None, pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    user_id: Optional[str] = Field(default=None, max_length=255)
    username: Optional[str] = Field(default=None, max_length=320)
    email: Optional[str] = Field(default=None, max_length=320)
    display_name: Optional[str] = Field(default=None, max_length=320)
    session_id: Optional[str] = Field(default=None, pattern=r"^[0-9a-fA-F-]{36}$")
    request_id: Optional[str] = Field(default=None, pattern=r"^[A-Za-z0-9._-]{1,128}$")
    artifact_key: Optional[str] = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    run_id: Optional[str] = Field(default=None, pattern=r"^[0-9a-fA-F-]{36}$")
    reason_code: Optional[str] = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,79}$")
    duration_ms: Optional[int] = Field(default=None, ge=0, le=86_400_000)
    http_status: Optional[int] = Field(default=None, ge=100, le=599)


class UsageEventIngestResponse(BaseModel):
    status: str = "accepted"


class ArtifactExecutionRequest(BaseModel):
    model_config = {"extra": "forbid"}
    client_key: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    artifact_key: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    behavior: ArtifactExecutionBehavior
    output_formats: list[ArtifactOutputFormat] = Field(default_factory=list)
    query: Optional[dict[str, Any]] = None

    @model_validator(mode="after")
    def validate_formats(self):
        if ArtifactOutputFormat.csv in self.output_formats and self.query is None:
            raise ValueError("CSV output requires a database-owned execution query")
        return self
