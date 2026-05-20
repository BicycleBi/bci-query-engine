"""
engine.py — Core artifact run logic.

Flow:
  1. Read app.artifacts + app.templates from metadata DB
  2. Execute artifact.view_name against data DB
  3. Render Jinja2 template
  4. If delivery_mode in (email, both): read recipients, POST to email service
  5. Write log.artifact_runs
  6. Return run_id + status
"""
from datetime import datetime, timezone
from typing import Optional

from .db import get_data_conn, get_metadata_conn
from .renderer import render
from . import mailer as _mailer


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def run_artifact(
    client_key: str,
    artifact_key: str,
    mode: str = "email",
) -> dict:
    """
    Execute a single artifact run.

    mode values:
      email    — render + send email (honours delivery_mode)
      preview  — render only, return HTML in response body, no log
      dry-run  — render + log, no send
    """
    started_at = _now()
    run_id: Optional[str] = None
    artifact_id: Optional[str] = None

    try:
        with get_metadata_conn() as meta:
            # ── 1. Read artifact config ────────────────────────────────
            row = meta.execute(
                """
                SELECT
                    a.artifact_id,
                    a.view_name,
                    a.delivery_mode,
                    a.display_name  AS subject,
                    t.html_content  AS template_body
                FROM app.artifacts a
                JOIN app.templates t
                  ON t.template_id = a.template_id AND t.is_active
                WHERE a.client_key   = %s
                  AND a.artifact_key = %s
                  AND a.active
                """,
                (client_key, artifact_key),
            ).fetchone()

            if row is None:
                raise ValueError(
                    f"No active artifact found: client={client_key} artifact={artifact_key}"
                )

            artifact_id   = str(row[0])
            view_name     = row[1]
            delivery_mode = row[2]
            subject       = row[3]
            template_body = row[4]

            # ── 2. Query data DB ───────────────────────────────────────
            with get_data_conn() as data:
                cur = data.execute(f"SELECT * FROM {view_name}")  # noqa: S608
                cols = [d[0] for d in cur.description]
                data_rows = [dict(zip(cols, r)) for r in cur.fetchall()]

            # ── 3. Render ──────────────────────────────────────────────
            html = render(template_body, data_rows)

            # ── 4. preview mode — return HTML without logging or sending
            if mode == "preview":
                return {
                    "run_id": None,
                    "client_key": client_key,
                    "artifact_key": artifact_key,
                    "status": "preview",
                    "started_at": started_at,
                    "completed_at": _now(),
                    "preview_html": html,
                }

            # ── 5. Send email if applicable ────────────────────────────
            send_email = mode == "email" and delivery_mode in ("email", "both")
            recipient_count = 0
            if send_email:
                recipient_rows = meta.execute(
                    """
                    SELECT email, delivery_type
                    FROM app.artifact_recipients
                    WHERE artifact_id = %s AND active
                    ORDER BY delivery_type, email
                    """,
                    (artifact_id,),
                ).fetchall()
                to  = [r[0] for r in recipient_rows if r[1] == "to"]
                cc  = [r[0] for r in recipient_rows if r[1] == "cc"]
                bcc = [r[0] for r in recipient_rows if r[1] == "bcc"]
                recipient_count = len(recipient_rows)
                _mailer.send(
                    subject=subject,
                    html=html,
                    to=to,
                    cc=cc,
                    bcc=bcc,
                    client_key=client_key,
                    artifact_key=artifact_key,
                )

            # ── 6. Log the run ─────────────────────────────────────────
            completed_at = _now()
            run_id = str(
                meta.execute(
                    """
                    INSERT INTO log.artifact_runs
                        (artifact_id, artifact_key, client_key,
                         triggered_by, status, delivery_mode,
                         row_count, recipient_count,
                         started_at, completed_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING run_id
                    """,
                    (
                        artifact_id, artifact_key, client_key,
                        "api", "completed", delivery_mode,
                        len(data_rows), recipient_count,
                        started_at, completed_at,
                    ),
                ).fetchone()[0]
            )
            meta.commit()

            return {
                "run_id": run_id,
                "client_key": client_key,
                "artifact_key": artifact_key,
                "status": "success",
                "started_at": started_at,
                "completed_at": completed_at,
            }

    except Exception as exc:
        completed_at = _now()

        # Best-effort log: may fail if metadata DB is down
        try:
            with get_metadata_conn() as meta:
                aid = artifact_id or _lookup_artifact_id(meta, client_key, artifact_key)
                if aid:
                    run_id = str(
                        meta.execute(
                            """
                            INSERT INTO log.artifact_runs
                                (artifact_id, artifact_key, client_key,
                                 triggered_by, status,
                                 started_at, completed_at, error_message)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                            RETURNING run_id
                            """,
                            (
                                aid, artifact_key, client_key,
                                "api", "failed",
                                started_at, completed_at, str(exc),
                            ),
                        ).fetchone()[0]
                    )
                    meta.commit()
        except Exception:
            pass

        return {
            "run_id": run_id,
            "client_key": client_key,
            "artifact_key": artifact_key,
            "status": "error",
            "started_at": started_at,
            "completed_at": completed_at,
            "error_message": str(exc),
        }


def _lookup_artifact_id(conn, client_key: str, artifact_key: str) -> Optional[str]:
    row = conn.execute(
        "SELECT artifact_id FROM app.artifacts WHERE client_key=%s AND artifact_key=%s",
        (client_key, artifact_key),
    ).fetchone()
    return str(row[0]) if row else None


def get_run(run_id: str) -> Optional[dict]:
    """Fetch a previous run record from log.artifact_runs."""
    with get_metadata_conn() as meta:
        row = meta.execute(
            """
            SELECT
                r.run_id,
                r.client_key,
                r.artifact_key,
                r.status,
                r.started_at,
                r.completed_at,
                r.error_message
            FROM log.artifact_runs r
            WHERE r.run_id = %s
            """,
            (run_id,),
        ).fetchone()

    if row is None:
        return None

    keys = ["run_id", "client_key", "artifact_key", "status",
            "started_at", "completed_at", "error_message"]
    return dict(zip(keys, row))
