"""Best-effort writers for the shared BCI Usage Monitoring System.

Only request metadata and authenticated identity are accepted here. Request
and response bodies, query/filter payloads, rendered output, and credentials
must never be passed to this module.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import re
from typing import Any, Optional

from .db import get_metadata_conn


_WRITER_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="usage-monitoring")
_SAFE_REASON = re.compile(r"^[a-z][a-z0-9_]{0,79}$")


def safe_reason_code(value: Optional[str]) -> Optional[str]:
    if value and _SAFE_REASON.fullmatch(value):
        return value
    return None


def record_request_span(
    *,
    request_id: str,
    identity: Optional[dict[str, Any]],
    client_key: Optional[str],
    method: str,
    route_template: str,
    artifact_key: Optional[str],
    run_id: Optional[str],
    started_at: datetime,
    completed_at: datetime,
    duration_ms: int,
    response_status: int,
    reason_code: Optional[str] = None,
    upstream_response_ms: Optional[float] = None,
    database_ms: Optional[float] = None,
    render_ms: Optional[float] = None,
    cache_status: Optional[str] = None,
) -> None:
    """Persist one metadata-only request/response lifecycle when enabled."""
    try:
        with get_metadata_conn() as meta:
            relation = meta.execute(
                "SELECT to_regclass('monitoring.request_spans')"
            ).fetchone()
            if not relation or relation[0] is None:
                return
            identity = identity or {}
            outcome = (
                "denied" if response_status in {401, 403} else
                "failed" if response_status >= 400 else
                "completed"
            )
            email = str(identity.get("email") or "").strip() or None
            display_name = str(identity.get("display_name") or "").strip() or None
            username = email or display_name
            meta.execute(
                """
                INSERT INTO monitoring.request_spans (
                    request_id, service_name, client_key, user_id, username,
                    email, display_name, session_id, method, route_template,
                    artifact_key, run_id, started_at, completed_at, duration_ms,
                    response_status, outcome, reason_code, upstream_response_ms,
                    database_ms, render_ms, cache_status
                ) VALUES (
                    %s, 'query-engine', %s, %s, %s,
                    %s, %s, NULLIF(%s, '')::uuid, %s, %s,
                    %s, NULLIF(%s, '')::uuid, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s
                )
                ON CONFLICT (request_id) DO NOTHING
                """,
                (
                    request_id,
                    client_key,
                    str(identity.get("sub") or "").strip() or None,
                    username,
                    email,
                    display_name,
                    str(identity.get("session_id") or "").strip(),
                    method,
                    route_template,
                    artifact_key,
                    str(run_id or "").strip(),
                    started_at,
                    completed_at,
                    max(0, duration_ms),
                    response_status,
                    outcome,
                    safe_reason_code(reason_code),
                    upstream_response_ms,
                    database_ms,
                    render_ms,
                    cache_status,
                ),
            )
            meta.commit()
    except Exception:
        # Monitoring must never break an artifact request.
        return


def record_request_span_async(**values: Any) -> None:
    _WRITER_POOL.submit(record_request_span, **values)


def record_interaction_event(
    *,
    identity: dict[str, Any],
    client_key: str,
    artifact_key: str,
    event_type: str,
    event_key: str,
    event_status: str,
    request_id: Optional[str],
    duration_ms: Optional[int],
    reason_code: Optional[str],
) -> None:
    """Persist one allowlisted, payload-free dashboard interaction."""
    try:
        with get_metadata_conn() as meta:
            relation = meta.execute("SELECT to_regclass('monitoring.events')").fetchone()
            if not relation or relation[0] is None:
                return
            email = str(identity.get("email") or "").strip() or None
            display_name = str(identity.get("display_name") or "").strip() or None
            meta.execute(
                """
                INSERT INTO monitoring.events (
                    service_name, event_type, event_key, event_status, client_key,
                    user_id, username, email, display_name, session_id,
                    request_id, artifact_key, reason_code, duration_ms
                ) VALUES (
                    'query-engine', %s, %s, %s, %s,
                    %s, %s, %s, %s, NULLIF(%s, '')::uuid,
                    %s, %s, %s, %s
                )
                """,
                (
                    event_type,
                    event_key,
                    event_status,
                    client_key,
                    str(identity.get("sub") or "").strip() or None,
                    email or display_name,
                    email,
                    display_name,
                    str(identity.get("session_id") or "").strip(),
                    request_id,
                    artifact_key,
                    safe_reason_code(reason_code),
                    duration_ms,
                ),
            )
            meta.commit()
    except Exception:
        return


def record_interaction_event_async(**values: Any) -> None:
    _WRITER_POOL.submit(record_interaction_event, **values)
