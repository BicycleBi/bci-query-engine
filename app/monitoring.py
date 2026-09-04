"""Best-effort writers for the shared BCI Usage Monitoring System.

Only request metadata and authenticated identity are accepted here. Request
and response bodies, query/filter payloads, rendered output, and credentials
must never be passed to this module.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import logging
import os
import re
import socket
import threading
from typing import Any, Optional

from .db import get_metadata_conn


_WRITER_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="usage-monitoring")
_SAFE_REASON = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_SAFE_METHOD = re.compile(r"^[A-Z]{3,12}$")
_GATEWAY_FIELDS = {
    "timestamp",
    "request_id",
    "method",
    "uri",
    "status",
    "request_time_seconds",
    "upstream_connect_seconds",
    "upstream_header_seconds",
    "upstream_response_seconds",
    "upstream_status",
}
_GATEWAY_STOP = threading.Event()
_GATEWAY_THREAD: Optional[threading.Thread] = None
logger = logging.getLogger("bci-query-engine.monitoring")


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
                ON CONFLICT (request_id) DO UPDATE SET
                    service_name = EXCLUDED.service_name,
                    client_key = COALESCE(EXCLUDED.client_key, monitoring.request_spans.client_key),
                    user_id = COALESCE(EXCLUDED.user_id, monitoring.request_spans.user_id),
                    username = COALESCE(EXCLUDED.username, monitoring.request_spans.username),
                    email = COALESCE(EXCLUDED.email, monitoring.request_spans.email),
                    display_name = COALESCE(EXCLUDED.display_name, monitoring.request_spans.display_name),
                    session_id = COALESCE(EXCLUDED.session_id, monitoring.request_spans.session_id),
                    method = EXCLUDED.method,
                    route_template = EXCLUDED.route_template,
                    artifact_key = COALESCE(EXCLUDED.artifact_key, monitoring.request_spans.artifact_key),
                    run_id = COALESCE(EXCLUDED.run_id, monitoring.request_spans.run_id),
                    started_at = EXCLUDED.started_at,
                    completed_at = EXCLUDED.completed_at,
                    duration_ms = EXCLUDED.duration_ms,
                    response_status = EXCLUDED.response_status,
                    outcome = EXCLUDED.outcome,
                    reason_code = EXCLUDED.reason_code,
                    database_ms = EXCLUDED.database_ms,
                    render_ms = EXCLUDED.render_ms,
                    cache_status = EXCLUDED.cache_status
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


def record_ingested_event(**values: Any) -> None:
    """Persist a bounded event submitted by another trusted stack service."""
    try:
        with get_metadata_conn() as meta:
            relation = meta.execute("SELECT to_regclass('monitoring.events')").fetchone()
            if not relation or relation[0] is None:
                return
            meta.execute(
                """
                INSERT INTO monitoring.events (
                    service_name, event_type, event_status, client_key,
                    user_id, username, email, display_name, session_id,
                    request_id, artifact_key, run_id, reason_code,
                    duration_ms, http_status
                ) VALUES (
                    %s, %s, %s, %s,
                    %s, %s, %s, %s, NULLIF(%s, '')::uuid,
                    %s, %s, NULLIF(%s, '')::uuid, %s,
                    %s, %s
                )
                """,
                (
                    values.get("source_service"),
                    values.get("event_type"),
                    values.get("event_status"),
                    values.get("client_key"),
                    values.get("user_id"),
                    values.get("username"),
                    values.get("email"),
                    values.get("display_name"),
                    values.get("session_id") or "",
                    values.get("request_id"),
                    values.get("artifact_key"),
                    values.get("run_id") or "",
                    safe_reason_code(values.get("reason_code")),
                    values.get("duration_ms"),
                    values.get("http_status"),
                ),
            )
            meta.commit()
    except Exception:
        return


def record_ingested_event_async(**values: Any) -> None:
    _WRITER_POOL.submit(record_ingested_event, **values)


def _seconds_to_ms(value: Any) -> Optional[float]:
    try:
        text = str(value or "").strip()
        if not text or text == "-":
            return None
        seconds = float(text.split(",", 1)[0])
        if seconds < 0 or seconds > 86_400:
            return None
        return round(seconds * 1000, 3)
    except (TypeError, ValueError):
        return None


def parse_gateway_record(raw: bytes) -> Optional[dict[str, Any]]:
    """Parse only the fixed metadata-only Nginx JSON contract."""
    try:
        text = raw.decode("utf-8", errors="strict")
        payload = json.loads(text[text.index("{"):])
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or set(payload) != _GATEWAY_FIELDS:
        return None
    request_id = str(payload.get("request_id") or "")
    method = str(payload.get("method") or "").upper()
    uri = str(payload.get("uri") or "")
    try:
        status = int(payload.get("status"))
        started_at = datetime.fromisoformat(str(payload.get("timestamp")).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if (
        not _SAFE_REQUEST_ID.fullmatch(request_id)
        or not _SAFE_METHOD.fullmatch(method)
        or not uri.startswith("/")
        or len(uri) > 512
        or "?" in uri
        or not 100 <= status <= 599
        or started_at.tzinfo is None
    ):
        return None
    gateway_duration_ms = _seconds_to_ms(payload.get("request_time_seconds"))
    if gateway_duration_ms is None:
        return None
    return {
        "request_id": request_id,
        "method": method,
        "route_path": uri,
        "response_status": status,
        "started_at": started_at.astimezone(timezone.utc),
        "gateway_duration_ms": gateway_duration_ms,
        "upstream_connect_ms": _seconds_to_ms(payload.get("upstream_connect_seconds")),
        "upstream_header_ms": _seconds_to_ms(payload.get("upstream_header_seconds")),
        "upstream_response_ms": _seconds_to_ms(payload.get("upstream_response_seconds")),
    }


def record_gateway_span(**values: Any) -> None:
    """Insert or enrich one request span from Nginx via Query Engine."""
    try:
        with get_metadata_conn() as meta:
            relation = meta.execute("SELECT to_regclass('monitoring.request_spans')").fetchone()
            if not relation or relation[0] is None:
                return
            duration_ms = int(values["gateway_duration_ms"])
            completed_at = values["started_at"] + timedelta(milliseconds=duration_ms)
            status = int(values["response_status"])
            outcome = "denied" if status in {401, 403} else "failed" if status >= 400 else "completed"
            meta.execute(
                """
                INSERT INTO monitoring.request_spans (
                    request_id, service_name, method, route_template,
                    started_at, completed_at, duration_ms, response_status,
                    outcome, gateway_duration_ms, upstream_connect_ms,
                    upstream_header_ms, upstream_response_ms
                ) VALUES (
                    %s, 'nginx', %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s
                )
                ON CONFLICT (request_id) DO UPDATE SET
                    gateway_duration_ms = EXCLUDED.gateway_duration_ms,
                    upstream_connect_ms = EXCLUDED.upstream_connect_ms,
                    upstream_header_ms = EXCLUDED.upstream_header_ms,
                    upstream_response_ms = EXCLUDED.upstream_response_ms
                """,
                (
                    values["request_id"], values["method"], values["route_path"],
                    values["started_at"], completed_at, duration_ms, status,
                    outcome, values["gateway_duration_ms"], values.get("upstream_connect_ms"),
                    values.get("upstream_header_ms"), values.get("upstream_response_ms"),
                ),
            )
            meta.commit()
    except Exception:
        return


def _gateway_listener() -> None:
    host = os.getenv("USAGE_MONITORING_SYSLOG_HOST", "0.0.0.0")
    port = int(os.getenv("USAGE_MONITORING_SYSLOG_PORT", "8514"))
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((host, port))
        listener.settimeout(0.5)
        while not _GATEWAY_STOP.is_set():
            try:
                raw, _address = listener.recvfrom(4096)
            except socket.timeout:
                continue
            record = parse_gateway_record(raw)
            if record is not None:
                _WRITER_POOL.submit(record_gateway_span, **record)


def start_gateway_listener() -> None:
    global _GATEWAY_THREAD
    if os.getenv("USAGE_MONITORING_SYSLOG_ENABLED", "true").lower() not in {"1", "true", "yes", "on"}:
        return
    if _GATEWAY_THREAD and _GATEWAY_THREAD.is_alive():
        return
    _GATEWAY_STOP.clear()
    _GATEWAY_THREAD = threading.Thread(target=_gateway_listener, name="usage-gateway", daemon=True)
    _GATEWAY_THREAD.start()


def stop_gateway_listener() -> None:
    _GATEWAY_STOP.set()
