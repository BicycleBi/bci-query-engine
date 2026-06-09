"""
mailer.py — Sends rendered HTML via the bci-email-service REST API.
"""
import base64
import os
from pathlib import Path
from typing import Optional

import requests


def _email_service_timeout_seconds() -> float:
    return float(os.environ.get("EMAIL_SERVICE_TIMEOUT_SECONDS", "90"))


def send(
    subject: str,
    html: str,
    to: list[str],
    *,
    cc: Optional[list[str]] = None,
    bcc: Optional[list[str]] = None,
    client_key: Optional[str] = None,
    artifact_key: Optional[str] = None,
    run_id: Optional[str] = None,
    attachments: Optional[list[dict]] = None,
) -> dict:
    """
    POST the rendered report to the email service.

    Returns the JSON response body from the email service.
    Raises requests.HTTPError on non-2xx responses.
    """
    url = os.environ.get("EMAIL_SERVICE_URL", "http://email-service:8200")
    service_token = os.environ.get("SERVICE_TOKEN", "")
    if not service_token:
        raise ValueError("SERVICE_TOKEN is required for email delivery")

    payload = {
        "subject": subject,
        "html": html,
        "to": to,
        "cc": cc or [],
        "bcc": bcc or [],
        "client_key": client_key,
        "artifact_key": artifact_key,
        "run_id": run_id,
        "attachments": [_attachment_payload(attachment) for attachment in attachments or []],
    }
    headers = {"Authorization": f"Bearer {service_token}"}
    resp = requests.post(
        f"{url}/send",
        json=payload,
        headers=headers,
        timeout=_email_service_timeout_seconds(),
    )
    resp.raise_for_status()
    return resp.json()


def _attachment_payload(attachment: dict) -> dict:
    path = Path(attachment["storage_path"])
    return {
        "name": attachment["filename"],
        "content_type": attachment.get("content_type") or "application/octet-stream",
        "content_bytes": base64.b64encode(path.read_bytes()).decode("ascii"),
        "size_bytes": attachment.get("file_size_bytes") or path.stat().st_size,
    }
