"""
models.py — Pydantic request/response models for the Query Engine API.
"""
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class RunMode(str, Enum):
    email = "email"
    preview = "preview"
    dry_run = "dry-run"


class ArtifactExecutionBehavior(str, Enum):
    deliver = "deliver"
    display = "display"
    dry_run = "dry-run"


class DeliveryMode(str, Enum):
    email = "email"
    web = "web"
    both = "both"


class TemplateContentType(str, Enum):
    html = "html"
    text = "text"


class RecipientDeliveryType(str, Enum):
    to = "to"
    cc = "cc"
    bcc = "bcc"


class ArtifactReferenceRole(str, Enum):
    body = "body"
    attachment = "attachment"


class ArtifactOutputFormat(str, Enum):
    html = "html"
    pdf = "pdf"
    xlsx = "xlsx"
    csv = "csv"
    txt = "txt"


class TemplateWriteRequest(BaseModel):
    template_key: str
    version: int = 1
    display_name: Optional[str] = None
    content_type: TemplateContentType = TemplateContentType.html
    html_content: str
    is_active: bool = True


class ArtifactRecipientWriteRequest(BaseModel):
    email: str
    delivery_type: RecipientDeliveryType = RecipientDeliveryType.to
    active: bool = True


class ArtifactReferenceWriteRequest(BaseModel):
    referenced_artifact_key: str
    reference_role: ArtifactReferenceRole = ArtifactReferenceRole.body
    output_format: ArtifactOutputFormat = ArtifactOutputFormat.html
    active: bool = True


class ArtifactWriteRequest(BaseModel):
    client_key: str
    artifact_key: str
    view_name: Optional[str] = None
    delivery_mode: DeliveryMode
    template: Optional[TemplateWriteRequest] = None
    client_display_name: Optional[str] = None
    display_name: Optional[str] = None
    description: Optional[str] = None
    active: bool = True
    recipients: list[ArtifactRecipientWriteRequest] = Field(default_factory=list)
    references: list[ArtifactReferenceWriteRequest] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_definition(self) -> "ArtifactWriteRequest":
        has_source = self.view_name is not None and self.template is not None
        source_is_empty = self.view_name is None and self.template is None
        body_reference_count = sum(1 for ref in self.references if ref.reference_role == ArtifactReferenceRole.body)

        if not has_source and not source_is_empty:
            raise ValueError("Artifact source requires both view_name and template")

        if not has_source and not self.references:
            raise ValueError("Artifact definition requires a direct source or at least one artifact reference")

        if self.delivery_mode == DeliveryMode.web and not has_source:
            raise ValueError("Web artifacts must define their own source and template")

        if body_reference_count > 1:
            raise ValueError("Only one body artifact reference is currently supported")

        return self


class ArtifactWriteResponse(BaseModel):
    artifact_id: str
    template_id: Optional[str] = None
    client_key: str
    artifact_key: str
    status: str
    recipient_count: int
    reference_count: int


class ArtifactExecutionRequest(BaseModel):
    client_key: str
    artifact_key: str
    behavior: ArtifactExecutionBehavior = ArtifactExecutionBehavior.deliver
    output_formats: list[ArtifactOutputFormat] = Field(default_factory=list)
    query: Optional[dict[str, Any]] = None
    distribution_group_keys: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("distribution_group_keys")
    @classmethod
    def validate_distribution_group_keys(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            key = value.strip()
            if not key or len(key) > 100:
                raise ValueError("Distribution group keys must be between 1 and 100 characters")
            if not all(character.isalnum() or character in "-_" for character in key):
                raise ValueError(
                    "Distribution group keys may contain only letters, numbers, hyphens, and underscores"
                )
            if key not in normalized:
                normalized.append(key)
        return normalized

    @model_validator(mode="after")
    def validate_execution_options(self) -> "ArtifactExecutionRequest":
        if self.distribution_group_keys and self.behavior != ArtifactExecutionBehavior.deliver:
            raise ValueError("Distribution groups are supported only for delivery executions")
        if ArtifactOutputFormat.csv in self.output_formats and self.query is None:
            raise ValueError("CSV output requires a database-owned execution query")
        return self


class ArtifactQueryCachePrewarmRequest(BaseModel):
    queries: list[dict[str, Any]] = Field(min_length=1, max_length=16)


class ArtifactQueryCachePrewarmResponse(BaseModel):
    client_key: str
    artifact_key: str
    status: str
    entry_count: int


class ArtifactExecutionResponse(BaseModel):
    run_id: Optional[str] = None
    client_key: str
    artifact_key: str
    status: str
    started_at: datetime
    completed_at: Optional[datetime] = None
    error_message: Optional[str] = None
    preview_html: Optional[str] = None
    outputs: list[dict] = Field(default_factory=list)

    @field_validator("run_id", mode="before")
    @classmethod
    def normalize_run_id(cls, value: Any) -> Optional[str]:
        return None if value is None else str(value)


class RunResponse(ArtifactExecutionResponse):
    pass


class HealthResponse(BaseModel):
    status: str
    service: str = "bci-query-engine"
