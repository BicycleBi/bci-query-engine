"""Fail-closed per-stack configuration for BCI Analytics reporting."""
from dataclasses import dataclass
import os

from fastapi import HTTPException


@dataclass(frozen=True)
class AnalyticsReportingConfig:
    client_key: str
    admin_role: str
    client_reporting_role: str = ""

    @property
    def audiences(self) -> set[str]:
        return {"all", self.client_key, "bicycle"}


def get_analytics_reporting_config(
    client_key: str,
    *,
    required: bool = True,
) -> AnalyticsReportingConfig | None:
    """Return the current stack's reporting contract without crossing clients."""
    expected_client = os.getenv("ANALYTICS_REPORTING_CLIENT_KEY", "").strip()
    admin_role = os.getenv("ANALYTICS_BICYCLE_ADMIN_ROLE", "").strip()
    client_role = os.getenv("ANALYTICS_CLIENT_REPORTING_ROLE", "").strip()
    generic_configured = bool(expected_client or admin_role or client_role)

    if generic_configured:
        if expected_client == client_key and admin_role:
            return AnalyticsReportingConfig(expected_client, admin_role, client_role)
        if required:
            raise HTTPException(503, "Analytics reporting authorization is unavailable")
        return None

    if required:
        raise HTTPException(503, "Analytics reporting authorization is unavailable")
    return None
