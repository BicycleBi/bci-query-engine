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
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any, Optional

from .db import get_data_conn, get_metadata_conn


def _delivery_worker_count() -> int:
    try:
        return max(1, int(os.getenv("ARTIFACT_DELIVERY_WORKERS", "2")))
    except ValueError:
        return 2


def _delivery_lease_seconds() -> int:
    try:
        return max(600, int(os.getenv("ARTIFACT_DELIVERY_LEASE_SECONDS", "1200")))
    except ValueError:
        return 1200


_DELIVERY_EXECUTION_SLOTS = threading.BoundedSemaphore(_delivery_worker_count())


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _safe_filename_part(value: Any, fallback: str = "artifact") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9 ._-]+", "", str(value or fallback)).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:120] or fallback


def _row_slice_label(row: dict[str, Any], fallback: str) -> str:
    for key in ("facility_name", "practice_name", "slice_name", "slice_key", "facility_id"):
        value = row.get(key)
        if value:
            return str(value)
    return fallback


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_output_dir() -> Path:
    return Path(os.getenv("ARTIFACT_OUTPUT_DIR", "/tmp/bci-query-engine/artifact-outputs"))


def _chromium_executable() -> str:
    configured = os.getenv("PDF_CHROMIUM_EXECUTABLE")
    candidates = [configured] if configured else []
    candidates.extend(["chromium", "chromium-browser", "google-chrome", "google-chrome-stable"])
    for candidate in candidates:
        if candidate and shutil.which(candidate):
            return candidate
    raise RuntimeError(
        "PDF output requested, but no Chromium executable was found. "
        "Set PDF_CHROMIUM_EXECUTABLE or install chromium in the query-engine image."
    )


def _render_pdf_from_html(html: str, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    executable = _chromium_executable()
    with tempfile.NamedTemporaryFile("w", suffix=".html", encoding="utf-8", delete=False) as handle:
        handle.write(html)
        html_path = Path(handle.name)
    try:
        subprocess.run(
            [
                executable,
                "--headless",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--no-sandbox",
                f"--print-to-pdf={output_path}",
                str(html_path),
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=int(os.getenv("PDF_RENDER_TIMEOUT_SECONDS", "120")),
        )
    finally:
        html_path.unlink(missing_ok=True)


def _ensure_artifact_outputs_table(meta) -> None:
    meta.execute(
        """
        CREATE TABLE IF NOT EXISTS log.artifact_outputs (
            output_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            run_id UUID NOT NULL,
            artifact_id UUID,
            artifact_key TEXT NOT NULL,
            client_key TEXT NOT NULL,
            output_format TEXT NOT NULL,
            output_role TEXT,
            slice_key TEXT,
            slice_label TEXT,
            filename TEXT NOT NULL,
            storage_path TEXT NOT NULL,
            content_type TEXT NOT NULL,
            file_size_bytes BIGINT NOT NULL,
            sha256 TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'completed',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    meta.execute(
        """
        CREATE INDEX IF NOT EXISTS artifact_outputs_run_idx
        ON log.artifact_outputs (run_id)
        """
    )
    meta.execute(
        """
        CREATE INDEX IF NOT EXISTS artifact_outputs_artifact_idx
        ON log.artifact_outputs (client_key, artifact_key, created_at DESC)
        """
    )


def _generate_pdf_outputs(
    *,
    run_id: str,
    artifact_id: str,
    client_key: str,
    artifact_key: str,
    subject: str,
    template_body: str,
    data_rows: list[dict[str, Any]],
    render,
    rendered_at: datetime,
) -> list[dict[str, Any]]:
    output_root = _artifact_output_dir() / client_key / artifact_key / run_id
    render_date = rendered_at.strftime("%Y-%m-%d")
    outputs: list[dict[str, Any]] = []
    rows_for_output = data_rows or [{}]

    for index, row in enumerate(rows_for_output, start=1):
        slice_label = _row_slice_label(row, fallback=f"slice-{index}")
        slice_key = row.get("slice_key") or row.get("facility_id") or slice_label
        filename = (
            f"{_safe_filename_part(subject or artifact_key)} - "
            f"{_safe_filename_part(slice_label, fallback=f'slice-{index}')} - "
            f"{render_date}.pdf"
        )
        pdf_path = output_root / filename
        html = render(template_body, [row])
        _render_pdf_from_html(html, pdf_path)
        outputs.append(
            {
                "run_id": run_id,
                "artifact_id": artifact_id,
                "artifact_key": artifact_key,
                "client_key": client_key,
                "output_format": "pdf",
                "output_role": "attachment",
                "slice_key": str(slice_key),
                "slice_label": str(slice_label),
                "filename": filename,
                "storage_path": str(pdf_path),
                "content_type": "application/pdf",
                "file_size_bytes": pdf_path.stat().st_size,
                "sha256": _sha256_file(pdf_path),
                "status": "completed",
            }
        )

    return outputs


def _insert_artifact_outputs(meta, outputs: list[dict[str, Any]]) -> None:
    if not outputs:
        return
    _ensure_artifact_outputs_table(meta)
    for output in outputs:
        meta.execute(
            """
            INSERT INTO log.artifact_outputs
                (run_id, artifact_id, artifact_key, client_key, output_format,
                 output_role, slice_key, slice_label, filename, storage_path,
                 content_type, file_size_bytes, sha256, status)
            VALUES
                (%s::uuid, %s::uuid, %s, %s, %s,
                 %s, %s, %s, %s, %s,
                 %s, %s, %s, %s)
            """,
            (
                output["run_id"],
                output["artifact_id"],
                output["artifact_key"],
                output["client_key"],
                output["output_format"],
                output.get("output_role"),
                output.get("slice_key"),
                output.get("slice_label"),
                output["filename"],
                output["storage_path"],
                output["content_type"],
                output["file_size_bytes"],
                output["sha256"],
                output["status"],
            ),
        )


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


def _delivery_target_mode_allowed(meta, artifact_id: str, mode: str) -> bool:
    row = meta.execute(
        """
        SELECT CASE
                 WHEN %s = 'live' THEN target.live_enabled
                 WHEN %s = 'internal_test' THEN target.internal_test_enabled
                 ELSE false
               END
        FROM app.artifact_delivery_targets target
        WHERE target.artifact_id = %s::uuid
          AND target.active
        """,
        (mode, mode, artifact_id),
    ).fetchone()
    return bool(row and row[0])


def _artifact_requires_batch(meta, artifact_id: str) -> bool:
    row = meta.execute(
        """
        SELECT COALESCE(bool_or(batch_only), false)
        FROM app.artifact_delivery_targets
        WHERE artifact_id = %s::uuid AND active
        """,
        (artifact_id,),
    ).fetchone()
    return bool(row and row[0])


def _internal_test_delivery_envelope(
    subject: str,
    html: str,
) -> tuple[list[tuple[str, str]], str, str]:
    """Override only recipients while preserving the production message exactly."""
    internal_recipients = [
        email.strip()
        for email in os.getenv("ARTIFACT_INTERNAL_TEST_RECIPIENTS", "").split(",")
        if email.strip()
    ]
    if not internal_recipients:
        raise ValueError("Internal test recipient allowlist is not configured")
    return [(email, "to") for email in internal_recipients], subject, html


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


def _artifact_cache_freshness_timestamp(data, client_key: str, artifact_key: str) -> Optional[str]:
    """Return an optional client-defined freshness timestamp for cache keys."""
    try:
        exists = data.execute(
            "SELECT to_regprocedure('public.bci_artifact_cache_freshness(text,text)') IS NOT NULL"
        ).fetchone()
        if not exists or not bool(exists[0]):
            return None

        row = data.execute(
            "SELECT public.bci_artifact_cache_freshness(%s, %s)::text",
            (client_key, artifact_key),
        ).fetchone()
        if row is None or row[0] is None:
            return None
        return str(row[0])
    except Exception:
        return None


def _subject_hash(authenticated_subject: Optional[str]) -> Optional[str]:
    if not authenticated_subject:
        return None
    return hashlib.sha256(authenticated_subject.encode("utf-8")).hexdigest()


def _authorization_context_hash(authorized_roles: Optional[list[str]]) -> Optional[str]:
    if not authorized_roles:
        return None
    normalized = json.dumps(sorted(set(authorized_roles)), separators=(",", ":"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _set_authorization_context(
    data,
    authenticated_subject: Optional[str],
    authorized_roles: Optional[list[str]],
) -> None:
    """Bind the trusted Security subject and freshly resolved roles to the transaction."""
    data.execute(
        "SELECT set_config('bci.authenticated_subject', %s, true)",
        (authenticated_subject or "",),
    )
    data.execute(
        "SELECT set_config('bci.authorized_roles', %s, true)",
        (json.dumps(sorted(set(authorized_roles or [])), separators=(",", ":")),),
    )


def _safe_view_name(view_name: str) -> str:
    """Allow only schema-qualified identifiers from trusted metadata."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?", view_name or ""):
        raise ValueError("Artifact data view name is not a safe identifier")
    return view_name


def _query_function_name(view_name: str) -> str:
    """Return the database-owned query contract associated with a render view."""
    return _safe_view_name(f"{_safe_view_name(view_name)}_query")


def _query_cache_scope_function_name(view_name: str) -> str:
    """Return the optional database-owned cache-scope contract."""
    return _safe_view_name(f"{_safe_view_name(view_name)}_query_cache_scope")


def _serialized_artifact_query(query: dict[str, Any]) -> str:
    if not isinstance(query, dict):
        raise ValueError("Artifact query must be a JSON object")
    serialized_query = json.dumps(query, separators=(",", ":"), sort_keys=True)
    max_query_bytes = int(os.getenv("ARTIFACT_QUERY_MAX_BYTES", "65536"))
    if len(serialized_query.encode("utf-8")) > max_query_bytes:
        raise ValueError(f"Artifact query exceeds the {max_query_bytes}-byte request limit")
    return serialized_query


def _artifact_query_cache_scope(data, view_name: str, serialized_query: str) -> str:
    """Resolve an artifact-owned cache scope, defaulting safely to identity."""
    scope_function = _query_cache_scope_function_name(view_name)
    function_exists = data.execute(
        "SELECT to_regprocedure(%s)",
        (f"{scope_function}(jsonb)",),
    ).fetchone()[0]
    if function_exists is None:
        return "identity"
    row = data.execute(
        f"SELECT {scope_function}(%s::jsonb)",  # noqa: S608
        (serialized_query,),
    ).fetchone()
    scope = str(row[0]).strip().lower() if row and row[0] is not None else ""
    if scope not in {"identity", "shared"}:
        raise ValueError(f"Artifact query cache scope is invalid: {scope or '<empty>'}")
    return scope


def _artifact_query_cache_params(
    *,
    query: dict[str, Any],
    freshness: Optional[str],
    cache_scope: str,
    authenticated_subject: Optional[str] = None,
    authorized_roles: Optional[list[str]] = None,
) -> dict[str, Any]:
    if cache_scope == "shared":
        return {
            "cache_version": 3,
            "cache_scope": "shared",
            "data_freshness_timestamp": freshness,
            "query": query,
        }
    return {
        "cache_version": 2,
        "data_freshness_timestamp": freshness,
        "authenticated_subject_hash": _subject_hash(authenticated_subject),
        "authorization_context_hash": _authorization_context_hash(authorized_roles),
        "query": query,
    }


def _execute_artifact_query_contract(data, view_name: str, serialized_query: str) -> Any:
    query_function = _query_function_name(view_name)
    function_signature = f"{query_function}(jsonb)"
    function_exists = data.execute(
        "SELECT to_regprocedure(%s)",
        (function_signature,),
    ).fetchone()[0]
    if function_exists is None:
        raise ValueError("Artifact has no database query contract")
    result = data.execute(
        f"SELECT {query_function}(%s::jsonb)",  # noqa: S608
        (serialized_query,),
    ).fetchone()[0]
    if isinstance(result, str):
        result = json.loads(result)
    return result


def get_artifact_asset(client_key: str, artifact_key: str, asset_path: str) -> dict[str, Any]:
    """Return one active package-owned artifact asset from Metadata."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,254}", asset_path or ""):
        raise ValueError("Artifact asset path is invalid")
    if ".." in asset_path.split("/"):
        raise ValueError("Artifact asset path is invalid")

    with get_metadata_conn() as meta:
        relation = meta.execute(
            "SELECT to_regclass('app.artifact_assets')"
        ).fetchone()
        if not relation or relation[0] is None:
            raise ValueError("Artifact asset registry is unavailable")

        row = meta.execute(
            """
            SELECT content, content_type, sha256
            FROM app.artifact_assets
            WHERE client_key = %s
              AND artifact_key = %s
              AND asset_path = %s
              AND active
            """,
            (client_key, artifact_key, asset_path),
        ).fetchone()
    if row is None:
        raise ValueError(
            f"No active artifact asset found: client={client_key} "
            f"artifact={artifact_key} asset={asset_path}"
        )
    return {
        "content": bytes(row[0]),
        "content_type": str(row[1]),
        "sha256": str(row[2]),
    }


def execute_artifact_query(
    client_key: str,
    artifact_key: str,
    *,
    query: dict[str, Any],
    authenticated_subject: Optional[str] = None,
    authorized_roles: Optional[list[str]] = None,
) -> Any:
    """Execute a database-owned artifact query without interpreting its payload."""
    serialized_query = _serialized_artifact_query(query)

    with get_metadata_conn() as meta:
        artifact = _fetch_artifact(meta, client_key=client_key, artifact_key=artifact_key)
        if artifact is None:
            raise ValueError(f"No active artifact found: client={client_key} artifact={artifact_key}")
        render_artifact = _resolve_render_artifact(meta, artifact)
        view_name = _safe_view_name(render_artifact["view_name"])

    from .cache import get_artifact_cache, get_cache_settings

    cache_settings = get_cache_settings()
    with get_data_conn() as data:
        _set_authorization_context(data, authenticated_subject, authorized_roles)
        freshness = _artifact_cache_freshness_timestamp(data, client_key, artifact_key)
        cache_scope = _artifact_query_cache_scope(data, view_name, serialized_query)
        cache = get_artifact_cache(cache_settings) if cache_settings.enabled else None
        cache_params = _artifact_query_cache_params(
            query=query,
            freshness=freshness,
            cache_scope=cache_scope,
            authenticated_subject=authenticated_subject,
            authorized_roles=authorized_roles,
        )
        cache_key = cache.build_key(client_key, artifact_key, "artifact-query", cache_params) if cache else None
        if cache and cache_key:
            try:
                cached = cache.get(cache_key)
                if cached is not None:
                    return cached
            except Exception:
                cache = None

        try:
            result = _execute_artifact_query_contract(data, view_name, serialized_query)
        except ValueError as exc:
            if str(exc) == "Artifact has no database query contract":
                raise ValueError(
                    f"Artifact has no database query contract: client={client_key} "
                    f"artifact={artifact_key}"
                ) from exc
            raise

        if cache and cache_key:
            try:
                cache.set(cache_key, result, ttl_seconds=cache_settings.ttl_seconds)
            except Exception:
                pass
        return result


def prewarm_artifact_query_cache(
    client_key: str,
    artifact_key: str,
    *,
    queries: list[dict[str, Any]],
) -> dict[str, Any]:
    """Rebuild the exact shared Redis entries declared by an artifact contract."""
    if not queries or len(queries) > 16:
        raise ValueError("Artifact query cache prewarm requires between 1 and 16 queries")

    serialized_queries = [(_serialized_artifact_query(query), query) for query in queries]
    with get_metadata_conn() as meta:
        artifact = _fetch_artifact(meta, client_key=client_key, artifact_key=artifact_key)
        if artifact is None:
            raise ValueError(f"No active artifact found: client={client_key} artifact={artifact_key}")
        render_artifact = _resolve_render_artifact(meta, artifact)
        view_name = _safe_view_name(render_artifact["view_name"])

    from .cache import get_artifact_cache, get_cache_settings

    cache_settings = get_cache_settings()
    if not cache_settings.enabled:
        raise ValueError("Artifact query cache prewarm requires Redis caching to be enabled")
    cache = get_artifact_cache(cache_settings)
    if cache is None:
        raise ValueError("Artifact query cache prewarm could not connect to Redis")

    entries: list[tuple[str, Any]] = []
    with get_data_conn() as data:
        _set_authorization_context(data, None, None)
        freshness = _artifact_cache_freshness_timestamp(data, client_key, artifact_key)
        for serialized_query, query in serialized_queries:
            cache_scope = _artifact_query_cache_scope(data, view_name, serialized_query)
            if cache_scope != "shared":
                raise ValueError("Artifact query cache prewarm accepts only shared query contracts")
            result = _execute_artifact_query_contract(data, view_name, serialized_query)
            cache_params = _artifact_query_cache_params(
                query=query,
                freshness=freshness,
                cache_scope="shared",
            )
            cache_key = cache.build_key(client_key, artifact_key, "artifact-query", cache_params)
            entries.append((cache_key, result))

    cache.invalidate_pattern(f"bci:cache:{client_key}:{artifact_key}:artifact-query:*")
    for cache_key, result in entries:
        cache.set(cache_key, result, ttl_seconds=cache_settings.ttl_seconds)

    return {
        "client_key": client_key,
        "artifact_key": artifact_key,
        "status": "prewarmed",
        "entry_count": len(entries),
    }


def execute_artifact(
    client_key: str,
    artifact_key: str,
    behavior: str = "deliver",
    output_formats: Optional[list[str]] = None,
    refresh_cache: bool = False,
    authenticated_subject: Optional[str] = None,
    authorized_roles: Optional[list[str]] = None,
    run_id: Optional[str] = None,
    started_at: Optional[datetime] = None,
    precreated_run: bool = False,
    delivery_mode_override: str = "live",
) -> dict:
    """
        Execute a single artifact behavior.

        behavior values:
            deliver  — render + send email if delivery metadata allows it, then log
            display  — render + return HTML, then log
            dry-run  — render + log, no send
            preview  — render only, return HTML, no log (legacy compatibility)
    """
    started_at = started_at or _now()
    run_id = run_id or str(uuid.uuid4())
    started_perf = perf_counter()
    artifact_id: Optional[str] = None
    output_formats = output_formats or []
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
        if precreated_run:
            with get_metadata_conn() as status_meta:
                status_meta.execute(
                    """
                    UPDATE log.artifact_runs
                    SET status = %s,
                        completed_at = NULL,
                        error_message = NULL
                    WHERE run_id = %s::uuid
                    """,
                    ("preparing", run_id),
                )
                status_meta.commit()

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
            cacheable_render = behavior in {"display", "preview"} and not output_formats
            cache_status = "bypass"
            cache_read_ms: Optional[float] = None
            data_query_ms: Optional[float] = None
            render_ms: Optional[float] = None
            data_freshness_timestamp: Optional[str] = None
            if cacheable_render and cache_settings.enabled:
                with get_data_conn() as data:
                    _set_authorization_context(data, authenticated_subject, authorized_roles)
                    data_freshness_timestamp = _artifact_cache_freshness_timestamp(data, client_key, artifact_key)
            cache = get_artifact_cache(cache_settings) if cacheable_render and cache_settings.enabled else None
            if not cacheable_render:
                cache_status = "bypass"
            elif not cache_settings.enabled or not cache_settings.cache_rendered:
                cache_status = "disabled"
            elif cache is None:
                cache_status = "unavailable"
            elif refresh_cache:
                cache_status = "refresh"
            cache_params = build_render_cache_params(
                behavior=behavior,
                view_name=view_name,
                template_body=template_body,
                template_id=render_template_id,
                render_artifact_id=render_artifact_id,
                authenticated_subject_hash=_subject_hash(authenticated_subject),
                authorization_context_hash=_authorization_context_hash(authorized_roles),
                data_freshness_timestamp=data_freshness_timestamp,
            )
            cached_render = None
            if cache is not None and cache_settings.cache_rendered and not refresh_cache:
                cache_read_started = perf_counter()
                cached_render = get_cached_render(cache, client_key, artifact_key, cache_params)
                cache_read_ms = (perf_counter() - cache_read_started) * 1000
                cache_status = "hit" if cached_render is not None else "miss"

            # ── 2. Query data DB ───────────────────────────────────────
            if cached_render is not None:
                html = cached_render["html"]
                row_count = int(cached_render.get("row_count", 0))
            else:
                data_query_started = perf_counter()
                with get_data_conn() as data:
                    _set_authorization_context(data, authenticated_subject, authorized_roles)
                    cur = data.execute(f"SELECT * FROM {view_name}")  # noqa: S608
                    cols = [d[0] for d in cur.description]
                    data_rows = [dict(zip(cols, r)) for r in cur.fetchall()]
                data_query_ms = (perf_counter() - data_query_started) * 1000

            if cached_render is None:
                # 3. Render
                render_started = perf_counter()
                html = render(template_body, data_rows)
                render_ms = (perf_counter() - render_started) * 1000
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
                    "outputs": [],
                    "cache": {
                        "status": cache_status,
                        "enabled": cache_settings.enabled,
                        "row_count": row_count,
                        "cache_read_ms": cache_read_ms,
                        "data_freshness_timestamp": data_freshness_timestamp,
                        "data_query_ms": data_query_ms,
                        "render_ms": render_ms,
                        "total_ms": (perf_counter() - started_perf) * 1000,
                    },
                }

            # ── 5. Generate requested file outputs ─────────────────────
            outputs: list[dict[str, Any]] = []
            if "pdf" in output_formats:
                outputs.extend(
                    _generate_pdf_outputs(
                        run_id=run_id,
                        artifact_id=artifact_id,
                        client_key=client_key,
                        artifact_key=artifact_key,
                        subject=subject,
                        template_body=template_body,
                        data_rows=data_rows,
                        render=render,
                        rendered_at=started_at,
                    )
                )

            # ── 6. Send email if applicable ────────────────────────────
            return_html = behavior == "display"
            send_email = behavior == "deliver" and delivery_mode in ("email", "both")
            recipient_count = 0
            if send_email:
                if delivery_mode_override == "internal_test":
                    recipient_rows, subject, html = _internal_test_delivery_envelope(
                        subject,
                        html,
                    )
                else:
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
                if precreated_run:
                    meta.execute(
                        """
                        UPDATE log.artifact_runs
                        SET status = %s,
                            recipient_count = %s,
                            lease_expires_at = NOW() + (%s * INTERVAL '1 second')
                        WHERE run_id = %s::uuid
                        """,
                        ("sending", recipient_count, _delivery_lease_seconds(), run_id),
                    )
                    meta.commit()
                delivery = _mailer.send(
                    subject=subject,
                    html=html,
                    to=to,
                    cc=cc,
                    bcc=bcc,
                    client_key=client_key,
                    artifact_key=artifact_key,
                    run_id=run_id,
                    request_id=run_id,
                    attachments=outputs,
                )
                meta.execute(
                    """
                    UPDATE log.artifact_runs
                    SET delivery_id = %s::uuid,
                        delivery_status = %s,
                        delivery_provider = %s,
                        provider_message_id = %s,
                        provider_status_code = %s
                    WHERE run_id = %s::uuid
                    """,
                    (
                        delivery.get("delivery_id"),
                        "provider_accepted" if delivery.get("status") == "sent" else delivery.get("status"),
                        delivery.get("provider"),
                        delivery.get("provider_message_id"),
                        delivery.get("status_code"),
                        run_id,
                    ),
                )
                meta.commit()
                if delivery.get("status") != "sent":
                    raise RuntimeError(
                        delivery.get("error_message")
                        or "Email Service did not accept the delivery"
                    )

            # ── 7. Log the run and generated outputs ───────────────────
            completed_at = _now()
            if precreated_run:
                meta.execute(
                    """
                    UPDATE log.artifact_runs
                    SET artifact_id = %s,
                        status = %s,
                        delivery_mode = %s,
                        row_count = %s,
                        slice_count = %s,
                        recipient_count = %s,
                        completed_at = %s,
                        error_message = NULL
                    WHERE run_id = %s::uuid
                    """,
                    (
                        artifact_id, "completed", delivery_mode,
                        row_count, len(outputs) if outputs else None, recipient_count,
                        completed_at, run_id,
                    ),
                )
            else:
                meta.execute(
                    """
                    INSERT INTO log.artifact_runs
                        (run_id, artifact_id, artifact_key, client_key,
                         triggered_by, status, delivery_mode,
                         row_count, slice_count, recipient_count,
                         started_at, completed_at)
                    VALUES (%s::uuid, %s, %s, %s,
                            %s, %s, %s,
                            %s, %s, %s,
                            %s, %s)
                    """,
                    (
                        run_id, artifact_id, artifact_key, client_key,
                        "api", "completed", delivery_mode,
                        row_count, len(outputs) if outputs else None, recipient_count,
                        started_at, completed_at,
                    ),
                )
            _insert_artifact_outputs(meta, outputs)
            if precreated_run:
                _update_delivery_batch_item(meta, run_id, "provider_accepted" if send_email else "completed")
            meta.commit()

            return {
                "run_id": run_id,
                "client_key": client_key,
                "artifact_key": artifact_key,
                "status": "success",
                "started_at": started_at,
                "completed_at": completed_at,
                "preview_html": html if return_html else None,
                "outputs": outputs,
                "cache": {
                    "status": cache_status,
                    "enabled": cache_settings.enabled,
                    "row_count": row_count,
                    "cache_read_ms": cache_read_ms,
                    "data_freshness_timestamp": data_freshness_timestamp,
                    "data_query_ms": data_query_ms,
                    "render_ms": render_ms,
                    "total_ms": (perf_counter() - started_perf) * 1000,
                },
            }

    except Exception as exc:
        completed_at = _now()

        # Best-effort log: may fail if metadata DB is down
        try:
            with get_metadata_conn() as meta:
                aid = artifact_id or _lookup_artifact_id(meta, client_key, artifact_key)
                if aid:
                    if precreated_run:
                        meta.execute(
                            """
                            UPDATE log.artifact_runs
                            SET artifact_id = %s,
                                status = %s,
                                completed_at = %s,
                                error_message = %s
                            WHERE run_id = %s::uuid
                            """,
                            (aid, "failed", completed_at, str(exc), run_id),
                        )
                    else:
                        run_id = str(
                            meta.execute(
                                """
                                INSERT INTO log.artifact_runs
                                    (run_id, artifact_id, artifact_key, client_key,
                                     triggered_by, status,
                                     started_at, completed_at, error_message)
                                VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, %s, %s)
                                RETURNING run_id
                                """,
                                (
                                    run_id, aid, artifact_key, client_key,
                                    "api", "failed",
                                    started_at, completed_at, str(exc),
                                ),
                            ).fetchone()[0]
                        )
                    if precreated_run:
                        _update_delivery_batch_item(meta, run_id, "failed", str(exc))
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
            "outputs": [],
        }


def _update_delivery_batch_item(meta, run_id: str, status: str, error_message: Optional[str] = None) -> None:
    meta.execute(
        """
        UPDATE log.artifact_delivery_batch_items
        SET status = %s,
            error_message = %s,
            completed_at = CASE WHEN %s IN ('provider_accepted', 'completed', 'failed') THEN NOW() ELSE NULL END,
            updated_at = NOW()
        WHERE run_id = %s::uuid
        """,
        (status, error_message, status, run_id),
    )
    meta.execute(
        """
        WITH target_batch AS (
            SELECT batch_id
            FROM log.artifact_delivery_batch_items
            WHERE run_id = %s::uuid
        ),
        latest AS (
            SELECT DISTINCT ON (artifact_key) artifact_key, status
            FROM log.artifact_delivery_batch_items
            WHERE batch_id = (SELECT batch_id FROM target_batch)
            ORDER BY artifact_key, attempt_number DESC, created_at DESC
        ),
        summary AS (
            SELECT
                count(*) AS total,
                count(*) FILTER (WHERE status IN ('queued', 'preparing', 'sending')) AS active,
                count(*) FILTER (WHERE status = 'failed') AS failed,
                count(*) FILTER (WHERE status = 'status_unknown') AS unknown,
                count(*) FILTER (WHERE status = 'delivered') AS delivered
            FROM latest
        )
        UPDATE log.artifact_delivery_batches batch
        SET status = CASE
                WHEN summary.active > 0 THEN 'in_progress'
                WHEN summary.unknown > 0 THEN 'attention_required'
                WHEN summary.failed = summary.total THEN 'failed'
                WHEN summary.failed > 0 THEN 'partially_failed'
                WHEN summary.delivered = summary.total THEN 'delivered'
                ELSE 'provider_accepted'
            END,
            completed_at = CASE WHEN summary.active > 0 THEN NULL ELSE NOW() END
        FROM target_batch, summary
        WHERE batch.batch_id = target_batch.batch_id
        """,
        (run_id,),
    )


def queue_artifact_execution(
    client_key: str,
    artifact_key: str,
    *,
    authenticated_subject: Optional[str] = None,
    authorized_roles: Optional[list[str]] = None,
    execution_mode: str = "live",
    run_id: Optional[str] = None,
) -> dict:
    """Persist an execution before asynchronous work begins."""
    run_id = run_id or str(uuid.uuid4())
    started_at = _now()
    with get_metadata_conn() as meta:
        artifact = _fetch_artifact(meta, client_key=client_key, artifact_key=artifact_key)
        if artifact is None:
            raise ValueError(
                f"No active artifact found: client={client_key} artifact={artifact_key}"
            )
        if _artifact_requires_batch(meta, artifact["artifact_id"]):
            raise ValueError(
                f"Artifact requires the governed delivery batch API: {artifact_key}"
            )
        meta.execute(
            """
            INSERT INTO log.artifact_runs
                (run_id, artifact_id, artifact_key, client_key,
                 triggered_by, status, delivery_mode, started_at,
                 authenticated_subject, authorized_roles, execution_mode)
            VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s::jsonb, %s)
            """,
            (
                run_id, artifact["artifact_id"], artifact_key, client_key,
                "api", "queued", artifact["delivery_mode"], started_at,
                authenticated_subject,
                json.dumps(sorted(set(authorized_roles or []))),
                execution_mode,
            ),
        )
        meta.commit()

    return {
        "run_id": run_id,
        "client_key": client_key,
        "artifact_key": artifact_key,
        "status": "queued",
        "started_at": started_at,
        "completed_at": None,
        "outputs": [],
    }


def execute_queued_artifact(*args: Any, **kwargs: Any) -> dict:
    """Run one queued artifact within the bounded delivery worker pool."""
    with _DELIVERY_EXECUTION_SLOTS:
        return execute_artifact(*args, **kwargs)


def claim_queued_artifact_execution() -> Optional[dict[str, Any]]:
    """Atomically lease one queued delivery so only one process can execute it."""
    with get_metadata_conn() as meta:
        row = meta.execute(
            """
            WITH candidate AS (
                SELECT run_id
                FROM log.artifact_runs
                WHERE status = 'queued'
                  AND delivery_mode IN ('email', 'both')
                ORDER BY started_at, run_id
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            UPDATE log.artifact_runs run
            SET status = 'preparing',
                lease_owner = %s,
                lease_expires_at = NOW() + (%s * INTERVAL '1 second'),
                attempt_count = COALESCE(attempt_count, 0) + 1
            FROM candidate
            WHERE run.run_id = candidate.run_id
            RETURNING run.run_id, run.client_key, run.artifact_key, run.started_at,
                      run.authenticated_subject, run.authorized_roles, run.execution_mode
            """,
            (f"query-engine:{os.getpid()}", _delivery_lease_seconds()),
        ).fetchone()
        meta.commit()
    if row is None:
        return None
    return {
        "run_id": str(row[0]),
        "client_key": row[1],
        "artifact_key": row[2],
        "started_at": row[3],
        "authenticated_subject": row[4],
        "authorized_roles": list(row[5] or []),
        "execution_mode": row[6] or "live",
    }


def run_delivery_worker() -> None:
    """Continuously execute durable queued deliveries; safe across process restarts."""
    poll_seconds = max(0.25, float(os.getenv("ARTIFACT_DELIVERY_POLL_SECONDS", "1")))
    while True:
        try:
            recover_expired_delivery_leases()
            claimed = claim_queued_artifact_execution()
            if claimed is None:
                time.sleep(poll_seconds)
                continue
            execute_queued_artifact(
                claimed["client_key"],
                claimed["artifact_key"],
                behavior="deliver",
                authenticated_subject=claimed["authenticated_subject"],
                authorized_roles=claimed["authorized_roles"],
                run_id=claimed["run_id"],
                started_at=claimed["started_at"],
                precreated_run=True,
                delivery_mode_override=claimed["execution_mode"],
            )
        except Exception:
            time.sleep(poll_seconds)


def recover_expired_delivery_leases() -> None:
    """Retry pre-send work only; quarantine a send whose outcome is uncertain."""
    with get_metadata_conn() as meta:
        meta.execute(
            """
            UPDATE log.artifact_runs
            SET status = 'queued', lease_owner = NULL, lease_expires_at = NULL
            WHERE status = 'preparing'
              AND lease_expires_at < NOW()
            """
        )
        uncertain = meta.execute(
            """
            UPDATE log.artifact_runs
            SET status = 'status_unknown',
                delivery_status = 'status_unknown',
                completed_at = NOW(),
                error_message = 'Worker lease expired after send began; manual reconciliation required'
            WHERE status = 'sending'
              AND lease_expires_at < NOW()
            RETURNING run_id
            """
        ).fetchall()
        for row in uncertain:
            _update_delivery_batch_item(
                meta,
                str(row[0]),
                "status_unknown",
                "Worker lease expired after send began; manual reconciliation required",
            )
        meta.commit()


def create_delivery_batch(
    client_key: str,
    artifact_keys: list[str],
    *,
    reporting_period: str,
    idempotency_key: str,
    mode: str,
    authenticated_subject: str,
    authorized_roles: list[str],
) -> dict[str, Any]:
    """Persist one logical owner-delivery batch and all of its durable queue items."""
    batch_id = str(uuid.uuid4())
    created_at = _now()
    with get_metadata_conn() as meta:
        existing = meta.execute(
            """
            SELECT batch_id
            FROM log.artifact_delivery_batches
            WHERE client_key = %s AND idempotency_key = %s
            """,
            (client_key, idempotency_key),
        ).fetchone()
        if existing:
            meta.commit()
            return get_delivery_batch(str(existing[0]))

        artifacts: list[dict[str, Any]] = []
        for artifact_key in artifact_keys:
            artifact = _fetch_artifact(meta, client_key=client_key, artifact_key=artifact_key)
            if artifact is None:
                raise ValueError(f"No active delivery target found: {artifact_key}")
            if artifact["delivery_mode"] not in {"email", "both"}:
                raise ValueError(f"Artifact is not configured for email delivery: {artifact_key}")
            if not _delivery_target_mode_allowed(meta, artifact["artifact_id"], mode):
                raise ValueError(
                    f"Artifact is not approved for {mode} owner delivery: {artifact_key}"
                )
            artifacts.append(artifact)

        try:
            meta.execute(
                """
                INSERT INTO log.artifact_delivery_batches
                    (batch_id, client_key, reporting_period, mode, idempotency_key,
                     status, requested_by, authorized_roles, created_at)
                VALUES (%s::uuid, %s, %s, %s, %s, 'queued', %s, %s::jsonb, %s)
                """,
                (
                    batch_id, client_key, reporting_period, mode, idempotency_key,
                    authenticated_subject, json.dumps(sorted(set(authorized_roles))), created_at,
                ),
            )
        except Exception as exc:
            if getattr(exc, "sqlstate", None) != "23505":
                raise
            meta.rollback()
            existing = meta.execute(
                """
                SELECT batch_id
                FROM log.artifact_delivery_batches
                WHERE client_key = %s AND idempotency_key = %s
                """,
                (client_key, idempotency_key),
            ).fetchone()
            if existing is None:
                raise
            return get_delivery_batch(str(existing[0]))
        for artifact in artifacts:
            run_id = str(uuid.uuid4())
            item_id = str(uuid.uuid4())
            meta.execute(
                """
                INSERT INTO log.artifact_runs
                    (run_id, artifact_id, artifact_key, client_key, triggered_by,
                     status, delivery_mode, started_at, authenticated_subject,
                     authorized_roles, execution_mode)
                VALUES (%s::uuid, %s::uuid, %s, %s, 'delivery-batch', 'queued',
                        %s, %s, %s, %s::jsonb, %s)
                """,
                (
                    run_id, artifact["artifact_id"], artifact["artifact_key"], client_key,
                    artifact["delivery_mode"], created_at, authenticated_subject,
                    json.dumps(sorted(set(authorized_roles))), mode,
                ),
            )
            meta.execute(
                """
                INSERT INTO log.artifact_delivery_batch_items
                    (item_id, batch_id, artifact_id, artifact_key, run_id,
                     status, attempt_number, created_at, updated_at)
                VALUES (%s::uuid, %s::uuid, %s::uuid, %s, %s::uuid,
                        'queued', 1, %s, %s)
                """,
                (
                    item_id, batch_id, artifact["artifact_id"], artifact["artifact_key"],
                    run_id, created_at, created_at,
                ),
            )
        meta.commit()
    result = get_delivery_batch(batch_id)
    if result is None:
        raise RuntimeError("Delivery batch could not be reloaded after creation")
    return result


def get_delivery_batch(batch_id: str) -> Optional[dict[str, Any]]:
    with get_metadata_conn() as meta:
        batch = meta.execute(
            """
            SELECT batch_id, client_key, reporting_period, mode, status, created_at, completed_at
            FROM log.artifact_delivery_batches
            WHERE batch_id = %s::uuid
            """,
            (batch_id,),
        ).fetchone()
        if batch is None:
            return None
        rows = meta.execute(
            """
            SELECT DISTINCT ON (item.artifact_key)
                   item.item_id, item.artifact_key, item.run_id,
                   COALESCE(run.delivery_status, run.status, item.status),
                   item.attempt_number, run.delivery_id, run.delivery_status,
                   COALESCE(run.error_message, item.error_message)
            FROM log.artifact_delivery_batch_items item
            JOIN log.artifact_runs run ON run.run_id = item.run_id
            WHERE item.batch_id = %s::uuid
            ORDER BY item.artifact_key, item.attempt_number DESC, item.created_at DESC
            """,
            (batch_id,),
        ).fetchall()
    items = [
        {
            "item_id": str(row[0]),
            "artifact_key": row[1],
            "run_id": str(row[2]),
            "status": row[3],
            "attempt_number": row[4],
            "delivery_id": str(row[5]) if row[5] else None,
            "delivery_status": row[6],
            "error_message": (
                "Delivery failed. Use the operator audit log for details."
                if row[7]
                else None
            ),
        }
        for row in rows
    ]
    statuses = {item["status"] for item in items}
    if any(status in {"queued", "preparing", "sending"} for status in statuses):
        status = "in_progress"
        completed_at = None
    elif "status_unknown" in statuses:
        status = "attention_required"
        completed_at = batch[6] or _now()
    elif "failed" in statuses:
        status = "failed" if statuses == {"failed"} else "partially_failed"
        completed_at = batch[6] or _now()
    elif statuses == {"delivered"}:
        status = "delivered"
        completed_at = batch[6] or _now()
    else:
        status = "provider_accepted"
        completed_at = batch[6] or _now()
    return {
        "batch_id": str(batch[0]),
        "client_key": batch[1],
        "reporting_period": batch[2],
        "mode": batch[3],
        "status": status,
        "created_at": batch[5],
        "completed_at": completed_at,
        "items": items,
    }


def retry_delivery_batch_item(
    batch_id: str,
    item_id: str,
    *,
    reason: str,
    authenticated_subject: str,
    authorized_roles: list[str],
) -> dict[str, Any]:
    """Create a new attempt only for a definitively failed delivery item."""
    now = _now()
    new_item_id = str(uuid.uuid4())
    new_run_id = str(uuid.uuid4())
    with get_metadata_conn() as meta:
        row = meta.execute(
            """
            SELECT batch.client_key, batch.mode, item.artifact_id, item.artifact_key,
                   item.attempt_number, run.status, run.delivery_status
            FROM log.artifact_delivery_batch_items item
            JOIN log.artifact_delivery_batches batch ON batch.batch_id = item.batch_id
            JOIN log.artifact_runs run ON run.run_id = item.run_id
            WHERE item.batch_id = %s::uuid AND item.item_id = %s::uuid
            """,
            (batch_id, item_id),
        ).fetchone()
        if row is None:
            raise ValueError("Delivery item was not found")
        terminal_status = row[6] or row[5]
        if terminal_status != "failed":
            raise ValueError(
                "Only definitively failed deliveries can be retried; reconcile unknown or accepted sends first"
            )
        attempt_number = int(row[4]) + 1
        meta.execute(
            """
            INSERT INTO log.artifact_runs
                (run_id, artifact_id, artifact_key, client_key, triggered_by,
                 status, delivery_mode, started_at, authenticated_subject,
                 authorized_roles, execution_mode)
            SELECT %s::uuid, artifact_id, artifact_key, client_key, 'delivery-retry',
                   'queued', delivery_mode, %s, %s, %s::jsonb, %s
            FROM log.artifact_runs
            WHERE run_id = (
                SELECT run_id FROM log.artifact_delivery_batch_items WHERE item_id = %s::uuid
            )
            """,
            (
                new_run_id, now, authenticated_subject,
                json.dumps(sorted(set(authorized_roles))), row[1], item_id,
            ),
        )
        meta.execute(
            """
            INSERT INTO log.artifact_delivery_batch_items
                (item_id, batch_id, artifact_id, artifact_key, run_id, status,
                 attempt_number, retry_reason, requested_by, created_at, updated_at)
            VALUES (%s::uuid, %s::uuid, %s::uuid, %s, %s::uuid, 'queued',
                    %s, %s, %s, %s, %s)
            """,
            (
                new_item_id, batch_id, row[2], row[3], new_run_id, attempt_number,
                reason, authenticated_subject, now, now,
            ),
        )
        _update_delivery_batch_item(meta, new_run_id, "queued")
        meta.commit()
    result = get_delivery_batch(batch_id)
    if result is None:
        raise RuntimeError("Delivery batch disappeared after retry")
    return result


def reconcile_delivery_batch(batch_id: str) -> dict[str, Any]:
    """Refresh delivery evidence without exposing recipients to Query Engine callers."""
    from . import mailer as _mailer

    with get_metadata_conn() as meta:
        rows = meta.execute(
            """
            SELECT run.run_id, run.delivery_id
            FROM log.artifact_delivery_batch_items item
            JOIN log.artifact_runs run ON run.run_id = item.run_id
            WHERE item.batch_id = %s::uuid
              AND run.delivery_id IS NOT NULL
              AND COALESCE(run.delivery_status, '') NOT IN ('delivered', 'failed', 'suppressed')
            """,
            (batch_id,),
        ).fetchall()
        for run_id, delivery_id in rows:
            try:
                delivery = _mailer.get_delivery(str(delivery_id))
            except Exception:
                continue
            provider_status = str(delivery.get("status") or "status_unknown")
            normalized = "provider_accepted" if provider_status == "sent" else provider_status
            meta.execute(
                """
                UPDATE log.artifact_runs
                SET delivery_status = %s,
                    provider_message_id = COALESCE(%s, provider_message_id),
                    provider_status_code = COALESCE(%s, provider_status_code),
                    status = CASE WHEN %s = 'failed' THEN 'failed' ELSE status END,
                    error_message = CASE WHEN %s = 'failed' THEN %s ELSE error_message END
                WHERE run_id = %s::uuid
                """,
                (
                    normalized,
                    delivery.get("provider_message_id"),
                    delivery.get("status_code"),
                    normalized,
                    normalized,
                    delivery.get("error_message"),
                    str(run_id),
                ),
            )
            _update_delivery_batch_item(
                meta,
                str(run_id),
                normalized,
                delivery.get("error_message") if normalized == "failed" else None,
            )
        meta.commit()
    result = get_delivery_batch(batch_id)
    if result is None:
        raise ValueError("Delivery batch not found")
    return result


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
                ,r.delivery_id
                ,r.delivery_status
                ,r.delivery_provider
                ,r.provider_message_id
                ,r.provider_status_code
            FROM log.artifact_runs r
            WHERE r.run_id = %s
            """,
            (run_id,),
        ).fetchone()
        _ensure_artifact_outputs_table(meta)
        outputs = meta.execute(
            """
            SELECT
                output_format,
                output_role,
                slice_key,
                slice_label,
                filename,
                storage_path,
                content_type,
                file_size_bytes,
                sha256,
                status,
                created_at
            FROM log.artifact_outputs
            WHERE run_id = %s
            ORDER BY created_at, filename
            """,
            (run_id,),
        ).fetchall()

    if row is None:
        return None

    keys = ["run_id", "client_key", "artifact_key", "status",
            "started_at", "completed_at", "error_message", "delivery_id",
            "delivery_status", "provider", "provider_message_id", "provider_status_code"]
    output_keys = [
        "output_format",
        "output_role",
        "slice_key",
        "slice_label",
        "filename",
        "storage_path",
        "content_type",
        "file_size_bytes",
        "sha256",
        "status",
        "created_at",
    ]
    result = dict(zip(keys, row))
    if result["run_id"] is not None:
        result["run_id"] = str(result["run_id"])
    result["outputs"] = [dict(zip(output_keys, output)) for output in outputs]
    return result
