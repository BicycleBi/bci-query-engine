"""Durable, authorization-gated client data-load actions.

The browser never calls Data Integration directly. Query Engine persists the
request, a bounded worker invokes the internal service with SERVICE_TOKEN, and
callers receive only a small non-sensitive status contract.
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import requests

try:
    from requests.exceptions import RequestException as RequestsRequestException
    from requests.exceptions import Timeout as RequestsTimeout
except (ImportError, AttributeError):  # pragma: no cover - lightweight test stubs
    class RequestsRequestException(Exception):
        pass

    class RequestsTimeout(RequestsRequestException):
        pass

from .db import get_metadata_conn


logger = logging.getLogger(__name__)


class DataLoadAlreadyRunning(RuntimeError):
    def __init__(self, action_id: str):
        super().__init__("A QuickBooks full load is already active")
        self.action_id = action_id


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _lease_seconds() -> int:
    try:
        return max(600, int(os.getenv("DATA_LOAD_ACTION_LEASE_SECONDS", "7200")))
    except ValueError:
        return 7200


def _poll_seconds() -> float:
    try:
        return max(0.25, float(os.getenv("DATA_LOAD_ACTION_POLL_SECONDS", "1")))
    except ValueError:
        return 1.0


def _timeout_seconds() -> int:
    try:
        return max(60, int(os.getenv("QUICKBOOKS_FULL_LOAD_TIMEOUT_SECONDS", "5400")))
    except ValueError:
        return 5400


def _ensure_table(meta) -> None:
    meta.execute(
        """
        CREATE TABLE IF NOT EXISTS log.data_load_actions (
            action_id UUID PRIMARY KEY,
            client_key TEXT NOT NULL,
            artifact_key TEXT NOT NULL,
            action_key TEXT NOT NULL,
            requested_by TEXT NOT NULL,
            authorized_roles JSONB NOT NULL DEFAULT '[]'::jsonb,
            status TEXT NOT NULL,
            error_code TEXT,
            requested_at TIMESTAMPTZ NOT NULL,
            started_at TIMESTAMPTZ,
            completed_at TIMESTAMPTZ,
            lease_owner TEXT,
            lease_expires_at TIMESTAMPTZ,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            progress JSONB NOT NULL DEFAULT '{}'::jsonb
        )
        """
    )
    meta.execute(
        """
        ALTER TABLE log.data_load_actions
        ADD COLUMN IF NOT EXISTS progress JSONB NOT NULL DEFAULT '{}'::jsonb
        """
    )
    meta.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS data_load_actions_one_active_idx
        ON log.data_load_actions (client_key, artifact_key, action_key)
        WHERE status IN ('queued', 'running')
        """
    )
    meta.execute(
        """
        CREATE INDEX IF NOT EXISTS data_load_actions_latest_idx
        ON log.data_load_actions (client_key, artifact_key, action_key, requested_at DESC)
        """
    )


def _safe_error_message(error_code: Optional[str]) -> Optional[str]:
    return {
        "already_running": "A QuickBooks load is already running.",
        "load_failed": "The QuickBooks load failed. The existing report data was retained.",
        "service_unavailable": "The QuickBooks load service is unavailable.",
        "worker_interrupted": "Load status is unavailable. Check the operator audit before retrying.",
        "timeout_unknown": "The load is taking longer than expected. Check the operator audit before retrying.",
        "unexpected_error": "The QuickBooks load could not be completed.",
    }.get(error_code)


def _row_result(row) -> dict[str, Any]:
    result = {
        "action_id": str(row[0]),
        "client_key": row[1],
        "artifact_key": row[2],
        "action_key": row[3],
        "status": row[4],
        "requested_at": row[5],
        "started_at": row[6],
        "completed_at": row[7],
        "error_message": _safe_error_message(row[8]),
    }
    result.update(_normalize_progress(row[9] if len(row) > 9 else None))
    return result


_SELECT_COLUMNS = """
    action_id, client_key, artifact_key, action_key, status,
    requested_at, started_at, completed_at, error_code, progress
"""


_PROGRESS_PHASES = {
    "running",
    "finalizing",
    "completed",
    "completed_with_warnings",
    "failed",
}
_FAILURE_MESSAGES = {
    "authorization_required": "QuickBooks authorization needs attention.",
    "quickbooks_timeout": "QuickBooks did not respond before the request timed out.",
    "quickbooks_rate_limited": "QuickBooks temporarily limited requests.",
    "quickbooks_unavailable": "QuickBooks is temporarily unavailable.",
    "quickbooks_request_failed": "QuickBooks could not complete this company refresh.",
    "processing_failed": "This company could not be refreshed.",
}


def _bounded_text(value: Any, length: int) -> Optional[str]:
    if not isinstance(value, str):
        return None
    value = " ".join(value.split()).strip()
    return value[:length] or None


def _bounded_count(value: Any) -> int:
    return max(0, min(500, value if isinstance(value, int) else 0))


def _normalize_progress(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    phase = value.get("phase") if value.get("phase") in _PROGRESS_PHASES else None
    failures = []
    for item in value.get("failures", []) if isinstance(value.get("failures"), list) else []:
        if not isinstance(item, dict):
            continue
        reason_code = item.get("reason_code")
        entity_name = _bounded_text(item.get("entity_name"), 120)
        if not entity_name or reason_code not in _FAILURE_MESSAGES:
            continue
        failures.append(
            {
                "entity_name": entity_name,
                "reason_code": reason_code,
                "message": _FAILURE_MESSAGES[reason_code],
            }
        )
        if len(failures) >= 50:
            break
    result = {
        "progress_phase": phase,
        "current_entity": _bounded_text(value.get("current_entity"), 120),
        "current_step": _bounded_text(value.get("current_step"), 180),
        "entities_completed": _bounded_count(value.get("entities_completed")),
        "entities_failed": _bounded_count(value.get("entities_failed")),
        "entities_total": _bounded_count(value.get("entities_total")),
        "progress_updated_at": _bounded_text(value.get("progress_updated_at"), 40),
        "entity_failures": failures,
    }
    return {key: item for key, item in result.items() if item is not None}


def _progress_storage(value: Any) -> dict[str, Any]:
    normalized = _normalize_progress(value)
    return {
        "phase": normalized.get("progress_phase"),
        "current_entity": normalized.get("current_entity"),
        "current_step": normalized.get("current_step"),
        "entities_completed": normalized.get("entities_completed", 0),
        "entities_failed": normalized.get("entities_failed", 0),
        "entities_total": normalized.get("entities_total", 0),
        "progress_updated_at": normalized.get("progress_updated_at"),
        "failures": normalized.get("entity_failures", []),
    }


def get_live_quickbooks_load_progress(action_id: str) -> dict[str, Any]:
    service_token = os.getenv("SERVICE_TOKEN", "").strip()
    url_template = os.getenv(
        "QUICKBOOKS_FULL_LOAD_PROGRESS_URL",
        "http://data-integration:8080/loads/srp-quickbooks-loader/progress/{action_id}",
    ).strip()
    if not service_token or "{action_id}" not in url_template:
        return {}
    try:
        response = requests.get(
            url_template.format(action_id=str(uuid.UUID(action_id))),
            headers={"Authorization": f"Bearer {service_token}"},
            timeout=5,
        )
        if response.status_code != 200:
            return {}
        return _normalize_progress(response.json())
    except (ValueError, RequestsRequestException):
        return {}


def queue_data_load_action(
    client_key: str,
    artifact_key: str,
    action_key: str,
    *,
    requested_by: str,
    authorized_roles: list[str],
) -> dict[str, Any]:
    action_id = str(uuid.uuid4())
    requested_at = _now()
    with get_metadata_conn() as meta:
        _ensure_table(meta)
        try:
            row = meta.execute(
                f"""
                INSERT INTO log.data_load_actions
                    (action_id, client_key, artifact_key, action_key, requested_by,
                     authorized_roles, status, requested_at)
                VALUES (%s::uuid, %s, %s, %s, %s, %s::jsonb, 'queued', %s)
                RETURNING {_SELECT_COLUMNS}
                """,
                (
                    action_id,
                    client_key,
                    artifact_key,
                    action_key,
                    requested_by,
                    json.dumps(sorted(set(authorized_roles))),
                    requested_at,
                ),
            ).fetchone()
            meta.commit()
        except Exception as exc:
            if getattr(exc, "sqlstate", None) != "23505":
                raise
            meta.rollback()
            _ensure_table(meta)
            active = meta.execute(
                """
                SELECT action_id
                FROM log.data_load_actions
                WHERE client_key = %s AND artifact_key = %s AND action_key = %s
                  AND status IN ('queued', 'running')
                ORDER BY requested_at DESC
                LIMIT 1
                """,
                (client_key, artifact_key, action_key),
            ).fetchone()
            meta.commit()
            raise DataLoadAlreadyRunning(str(active[0]) if active else "unknown") from exc
    return _row_result(row)


def get_data_load_action(
    action_id: str,
    client_key: str,
    artifact_key: str,
    action_key: str,
) -> Optional[dict[str, Any]]:
    with get_metadata_conn() as meta:
        _ensure_table(meta)
        row = meta.execute(
            f"""
            SELECT {_SELECT_COLUMNS}
            FROM log.data_load_actions
            WHERE action_id = %s::uuid
              AND client_key = %s AND artifact_key = %s AND action_key = %s
            """,
            (action_id, client_key, artifact_key, action_key),
        ).fetchone()
        meta.commit()
    return _row_result(row) if row else None


def get_latest_data_load_action(
    client_key: str,
    artifact_key: str,
    action_key: str,
) -> Optional[dict[str, Any]]:
    with get_metadata_conn() as meta:
        _ensure_table(meta)
        row = meta.execute(
            f"""
            SELECT {_SELECT_COLUMNS}
            FROM log.data_load_actions
            WHERE client_key = %s AND artifact_key = %s AND action_key = %s
            ORDER BY requested_at DESC
            LIMIT 1
            """,
            (client_key, artifact_key, action_key),
        ).fetchone()
        meta.commit()
    return _row_result(row) if row else None


def recover_expired_data_load_actions() -> None:
    """Quarantine an interrupted request; never replay a possibly active load."""
    with get_metadata_conn() as meta:
        _ensure_table(meta)
        meta.execute(
            """
            UPDATE log.data_load_actions
            SET status = 'attention_required',
                error_code = 'worker_interrupted',
                completed_at = NOW(),
                lease_owner = NULL,
                lease_expires_at = NULL
            WHERE status = 'running' AND lease_expires_at < NOW()
            """
        )
        meta.commit()


def claim_queued_data_load_action() -> Optional[dict[str, Any]]:
    with get_metadata_conn() as meta:
        _ensure_table(meta)
        row = meta.execute(
            """
            WITH candidate AS (
                SELECT action_id
                FROM log.data_load_actions
                WHERE status = 'queued'
                ORDER BY requested_at, action_id
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            UPDATE log.data_load_actions action
            SET status = 'running',
                started_at = COALESCE(started_at, NOW()),
                lease_owner = %s,
                lease_expires_at = NOW() + (%s * INTERVAL '1 second'),
                attempt_count = attempt_count + 1
            FROM candidate
            WHERE action.action_id = candidate.action_id
            RETURNING action.action_id, action.client_key, action.artifact_key,
                      action.action_key
            """,
            (f"query-engine:{os.getpid()}", _lease_seconds()),
        ).fetchone()
        meta.commit()
    if row is None:
        return None
    return {
        "action_id": str(row[0]),
        "client_key": row[1],
        "artifact_key": row[2],
        "action_key": row[3],
    }


def _finish_action(
    action_id: str,
    status: str,
    error_code: Optional[str] = None,
    progress: Optional[dict[str, Any]] = None,
) -> None:
    with get_metadata_conn() as meta:
        _ensure_table(meta)
        meta.execute(
            """
            UPDATE log.data_load_actions
            SET status = %s,
                error_code = %s,
                progress = COALESCE(%s::jsonb, progress),
                completed_at = NOW(),
                lease_owner = NULL,
                lease_expires_at = NULL
            WHERE action_id = %s::uuid AND status = 'running'
            """,
            (
                status,
                error_code,
                json.dumps(_progress_storage(progress)) if progress else None,
                action_id,
            ),
        )
        meta.commit()


def execute_data_load_action(action: dict[str, Any]) -> None:
    action_id = action["action_id"]
    if (
        action.get("client_key") != "srp"
        or action.get("artifact_key") != "quickbooks-profit-loss-report"
        or action.get("action_key") != "quickbooks-full-load"
    ):
        _finish_action(action_id, "failed", "unexpected_error")
        return

    url = os.getenv(
        "QUICKBOOKS_FULL_LOAD_URL",
        "http://data-integration:8080/loads/srp-quickbooks-loader/run",
    ).strip()
    service_token = os.getenv("SERVICE_TOKEN", "").strip()
    if not url or not service_token:
        _finish_action(action_id, "failed", "service_unavailable")
        return

    try:
        logger.info("QuickBooks load action dispatching correlation_id=%s", action_id)
        response = requests.post(
            url,
            json={},
            headers={
                "Authorization": f"Bearer {service_token}",
                "Content-Type": "application/json",
                "X-BCI-Trigger-Source": "manual",
                "X-BCI-Load-Scope": "full",
                "X-BCI-Correlation-ID": action_id,
            },
            timeout=_timeout_seconds(),
        )
        if response.status_code == 409:
            _finish_action(action_id, "failed", "already_running")
            logger.warning(
                "QuickBooks load action rejected correlation_id=%s http_status=%s",
                action_id,
                response.status_code,
            )
        elif response.status_code >= 400:
            _finish_action(action_id, "failed", "load_failed")
            logger.warning(
                "QuickBooks load action failed correlation_id=%s http_status=%s",
                action_id,
                response.status_code,
            )
        else:
            payload = response.json() if callable(getattr(response, "json", None)) else {}
            progress = payload.get("progress") if isinstance(payload, dict) else None
            loader_status = payload.get("status") if isinstance(payload, dict) else None
            if loader_status == "completed_with_warnings":
                _finish_action(
                    action_id,
                    "completed_with_warnings",
                    progress=progress,
                )
            elif loader_status == "failed":
                _finish_action(action_id, "failed", "load_failed", progress=progress)
            else:
                _finish_action(action_id, "completed", progress=progress)
            logger.info(
                "QuickBooks load action completed correlation_id=%s http_status=%s",
                action_id,
                response.status_code,
            )
    except RequestsTimeout:
        _finish_action(action_id, "attention_required", "timeout_unknown")
        logger.warning("QuickBooks load action timed out correlation_id=%s", action_id)
    except RequestsRequestException:
        _finish_action(action_id, "failed", "service_unavailable")
        logger.warning("QuickBooks load service unavailable correlation_id=%s", action_id)
    except Exception:
        _finish_action(action_id, "failed", "unexpected_error")
        logger.exception("QuickBooks load action failed unexpectedly correlation_id=%s", action_id)


def run_data_load_action_worker() -> None:
    while True:
        try:
            recover_expired_data_load_actions()
            action = claim_queued_data_load_action()
            if action is None:
                time.sleep(_poll_seconds())
                continue
            execute_data_load_action(action)
        except Exception:
            time.sleep(_poll_seconds())
