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
from typing import Any, Optional

from .db import get_data_conn, get_metadata_conn


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _fetch_artifact(
    meta,
    *,
    client_key: Optional[str] = None,
    artifact_key: Optional[str] = None,
    artifact_id: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    where_sql = "a.artifact_id = %s::uuid" if artifact_id else "a.client_key = %s AND a.artifact_key = %s"
    params: tuple[Any, ...] = (artifact_id,) if artifact_id else (client_key, artifact_key)
    row = meta.execute(
        f"""
        SELECT
            a.artifact_id,
            a.client_key,
            a.artifact_key,
            a.view_name,
            a.delivery_mode,
            a.display_name AS subject,
            t.html_content AS template_body,
            a.template_id
        FROM app.artifacts a
        LEFT JOIN app.templates t
          ON t.template_id = a.template_id AND t.is_active
        WHERE {where_sql}
          AND a.active
        """,
        params,
    ).fetchone()
    if row is None:
        return None

    keys = [
        "artifact_id",
        "client_key",
        "artifact_key",
        "view_name",
        "delivery_mode",
        "subject",
        "template_body",
        "template_id",
    ]
    artifact = dict(zip(keys, row))
    artifact["artifact_id"] = str(artifact["artifact_id"])
    artifact["template_id"] = str(artifact["template_id"]) if artifact["template_id"] else None
    return artifact


def _lookup_body_reference(meta, artifact_id: str) -> Optional[str]:
    row = meta.execute(
        """
        SELECT referenced_artifact_id
        FROM app.artifact_references
        WHERE artifact_id = %s::uuid
          AND reference_role = 'body'
          AND active
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (artifact_id,),
    ).fetchone()
    return str(row[0]) if row else None


def _resolve_render_artifact(meta, artifact: dict[str, Any], seen: Optional[set[str]] = None) -> dict[str, Any]:
    seen = seen or set()
    artifact_id = artifact["artifact_id"]

    if artifact_id in seen:
        raise ValueError(f"Cyclic artifact reference detected for artifact {artifact['artifact_key']}")

    if artifact.get("view_name") and artifact.get("template_body"):
        return artifact

    seen.add(artifact_id)
    referenced_artifact_id = _lookup_body_reference(meta, artifact_id)
    if referenced_artifact_id is None:
        raise ValueError(
            f"Artifact {artifact['artifact_key']} has no direct source and no active body reference"
        )

    referenced_artifact = _fetch_artifact(meta, artifact_id=referenced_artifact_id)
    if referenced_artifact is None:
        raise ValueError(
            f"Referenced artifact {referenced_artifact_id} could not be resolved for {artifact['artifact_key']}"
        )

    return _resolve_render_artifact(meta, referenced_artifact, seen)


def write_artifact_definition(definition: dict[str, Any]) -> dict[str, Any]:
    """Create or update an artifact definition in the metadata database."""
    template = definition.get("template")
    recipients = definition.get("recipients", [])
    references = definition.get("references", [])

    with get_metadata_conn() as meta:
        client_display_name = definition.get("client_display_name") or definition["client_key"]
        meta.execute(
            """
            INSERT INTO app.clients (client_key, display_name, is_active)
            VALUES (%s, %s, %s)
            ON CONFLICT (client_key) DO UPDATE
            SET display_name = EXCLUDED.display_name,
                is_active = EXCLUDED.is_active
            """,
            (definition["client_key"], client_display_name, True),
        )

        template_id: Optional[str] = None
        if template is not None:
            template_id = str(
                meta.execute(
                    """
                    INSERT INTO app.templates
                        (client_key, template_key, version, display_name,
                         content_type, html_content, is_active)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (client_key, template_key, version) DO UPDATE
                    SET display_name = EXCLUDED.display_name,
                        content_type = EXCLUDED.content_type,
                        html_content = EXCLUDED.html_content,
                        is_active = EXCLUDED.is_active
                    RETURNING template_id
                    """,
                    (
                        definition["client_key"],
                        template["template_key"],
                        template["version"],
                        template.get("display_name"),
                        template["content_type"],
                        template["html_content"],
                        template["is_active"],
                    ),
                ).fetchone()[0]
            )

            if template["is_active"]:
                meta.execute(
                    """
                    UPDATE app.templates
                    SET is_active = false
                    WHERE client_key = %s
                      AND template_key = %s
                      AND template_id <> %s::uuid
                    """,
                    (definition["client_key"], template["template_key"], template_id),
                )

        artifact_id = str(
            meta.execute(
                """
                INSERT INTO app.artifacts
                    (client_key, artifact_key, display_name, description,
                     view_name, delivery_mode, template_id, active, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s::uuid, %s, now())
                ON CONFLICT (client_key, artifact_key) DO UPDATE
                SET display_name = EXCLUDED.display_name,
                    description = EXCLUDED.description,
                    view_name = EXCLUDED.view_name,
                    delivery_mode = EXCLUDED.delivery_mode,
                    template_id = EXCLUDED.template_id,
                    active = EXCLUDED.active,
                    updated_at = now()
                RETURNING artifact_id
                """,
                (
                    definition["client_key"],
                    definition["artifact_key"],
                    definition.get("display_name"),
                    definition.get("description"),
                    definition.get("view_name"),
                    definition["delivery_mode"],
                    template_id,
                    definition.get("active", True),
                ),
            ).fetchone()[0]
        )

        meta.execute(
            "DELETE FROM app.artifact_recipients WHERE artifact_id = %s::uuid",
            (artifact_id,),
        )
        for recipient in recipients:
            meta.execute(
                """
                INSERT INTO app.artifact_recipients
                    (artifact_id, email, delivery_type, active)
                VALUES (%s::uuid, %s, %s, %s)
                """,
                (
                    artifact_id,
                    recipient["email"],
                    recipient["delivery_type"],
                    recipient.get("active", True),
                ),
            )

        meta.execute(
            "DELETE FROM app.artifact_references WHERE artifact_id = %s::uuid",
            (artifact_id,),
        )
        for reference in references:
            referenced_artifact_id = _lookup_artifact_id(
                meta,
                definition["client_key"],
                reference["referenced_artifact_key"],
            )
            if referenced_artifact_id is None:
                raise ValueError(
                    "Referenced artifact not found: "
                    f"client={definition['client_key']} artifact={reference['referenced_artifact_key']}"
                )

            meta.execute(
                """
                INSERT INTO app.artifact_references
                    (artifact_id, referenced_artifact_id, reference_role, output_format, active)
                VALUES (%s::uuid, %s::uuid, %s, %s, %s)
                """,
                (
                    artifact_id,
                    referenced_artifact_id,
                    reference["reference_role"],
                    reference["output_format"],
                    reference.get("active", True),
                ),
            )

        meta.commit()

    from .cache import invalidate_artifact_cache

    invalidate_artifact_cache(definition["client_key"], definition["artifact_key"])

    return {
        "artifact_id": artifact_id,
        "template_id": template_id,
        "client_key": definition["client_key"],
        "artifact_key": definition["artifact_key"],
        "status": "saved",
        "recipient_count": len(recipients),
        "reference_count": len(references),
    }


def execute_artifact(
    client_key: str,
    artifact_key: str,
    behavior: str = "deliver",
) -> dict:
    """
        Execute a single artifact behavior.

        behavior values:
            deliver  — render + send email if delivery metadata allows it, then log
            display  — render + return HTML, then log
            dry-run  — render + log, no send
            preview  — render only, return HTML, no log (legacy compatibility)
    """
    started_at = _now()
    run_id: Optional[str] = None
    artifact_id: Optional[str] = None
    from .renderer import render
    from . import mailer as _mailer
    from .cache import (
        build_render_cache_params,
        get_artifact_cache,
        get_cache_settings,
        get_cached_render,
        set_cached_render,
    )

    try:
        with get_metadata_conn() as meta:
            # ── 1. Read artifact config ────────────────────────────────
            artifact = _fetch_artifact(meta, client_key=client_key, artifact_key=artifact_key)

            if artifact is None:
                raise ValueError(
                    f"No active artifact found: client={client_key} artifact={artifact_key}"
                )

            artifact_id = artifact["artifact_id"]
            delivery_mode = artifact["delivery_mode"]
            subject = artifact["subject"]

            render_artifact = _resolve_render_artifact(meta, artifact)
            view_name = render_artifact["view_name"]
            template_body = render_artifact["template_body"]
            render_artifact_id = render_artifact["artifact_id"]
            render_template_id = render_artifact["template_id"]

            cache_settings = get_cache_settings()
            cacheable_render = behavior in {"display", "preview"}
            cache = get_artifact_cache(cache_settings) if cacheable_render and cache_settings.enabled else None
            cache_params = build_render_cache_params(
                behavior=behavior,
                view_name=view_name,
                template_body=template_body,
                template_id=render_template_id,
                render_artifact_id=render_artifact_id,
            )
            cached_render = (
                get_cached_render(cache, client_key, artifact_key, cache_params)
                if cache is not None and cache_settings.cache_rendered
                else None
            )

            # ── 2. Query data DB ───────────────────────────────────────
            if cached_render is not None:
                html = cached_render["html"]
                row_count = int(cached_render.get("row_count", 0))
            else:
                with get_data_conn() as data:
                    cur = data.execute(f"SELECT * FROM {view_name}")  # noqa: S608
                    cols = [d[0] for d in cur.description]
                    data_rows = [dict(zip(cols, r)) for r in cur.fetchall()]

            if cached_render is None:
                # 3. Render
                html = render(template_body, data_rows)
                row_count = len(data_rows)
                if cache is not None and cache_settings.cache_rendered:
                    set_cached_render(
                        cache,
                        client_key,
                        artifact_key,
                        cache_params,
                        html=html,
                        row_count=row_count,
                        ttl_seconds=cache_settings.ttl_seconds,
                    )

            # ── 4. Legacy preview mode — return HTML without logging or sending
            if behavior == "preview":
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
            return_html = behavior == "display"
            send_email = behavior == "deliver" and delivery_mode in ("email", "both")
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
                        row_count, recipient_count,
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
                "preview_html": html if return_html else None,
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


def run_artifact(
    client_key: str,
    artifact_key: str,
    mode: str = "email",
) -> dict:
    """Legacy execution wrapper for the historical /run API."""
    mode_to_behavior = {
        "email": "deliver",
        "preview": "preview",
        "dry-run": "dry-run",
    }
    return execute_artifact(client_key, artifact_key, behavior=mode_to_behavior[mode])


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
