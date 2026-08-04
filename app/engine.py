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
import uuid
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any, Optional

from .db import get_data_conn, get_metadata_conn


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


def _set_authenticated_subject(data, authenticated_subject: Optional[str]) -> None:
    """Bind the trusted Security subject to the current data transaction."""
    data.execute(
        "SELECT set_config('bci.authenticated_subject', %s, true)",
        (authenticated_subject or "",),
    )


_PRICING_FILTER_COLUMNS = {
    "type": "group_name",
    "subtype": "category",
    "sourceCategory": "category_name",
    "vehicleYear": "item_year",
    "vehicleMake": "item_make",
    "vehicleModel": "item_model",
    "age": "CASE WHEN NULLIF(item_age_at_sale, '') ~ '^[0-9]+(\\.[0-9]+)?$' THEN CONCAT(FLOOR(NULLIF(item_age_at_sale, '')::numeric / 5) * 5, ' to ', FLOOR(NULLIF(item_age_at_sale, '')::numeric / 5) * 5 + 4, ' years') END",
    "usage": "item_usage_type",
    "mileageBand": "item_mileage_band",
    "hoursBand": "item_hours_band",
    "measurement": "measurement",
    "amount": "amount",
}

# The display artifact stays compact; the interactive endpoint reads the
# complete detail projection through this explicit artifact data contract.
_INTERACTIVE_ARTIFACT_DATA_VIEWS = {
    ("rag", "pricing-intelligence-overview"): "public.rag_pricing_first_artifact_interactive_rows",
}

_PRICING_NUMERIC_PRICE = """
    CASE
        WHEN NULLIF(gross_transaction_value, '') ~ '^[0-9]+(\\.[0-9]+)?$'
        THEN NULLIF(gross_transaction_value, '')::numeric
    END
"""

_PRICING_NUMERIC_AGE = """
    CASE
        WHEN NULLIF(item_age_at_sale, '') ~ '^[0-9]+(\\.[0-9]+)?$'
        THEN NULLIF(item_age_at_sale, '')::numeric
    END
"""


def _rows_as_dicts(cursor) -> list[dict[str, Any]]:
    columns = [description[0] for description in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _pricing_chart_filter_sql(chart_selection: Optional[str]) -> tuple[str, list[str]]:
    """Return a trusted table-only filter for a dashboard chart selection."""
    selection = str(chart_selection or "").strip()
    if not selection:
        return "", []

    age_match = re.fullmatch(r"vehicle-age:([0-9]+)", selection)
    if age_match:
        return f" AND ROUND(({_PRICING_NUMERIC_AGE})) = %s", [age_match.group(1)]

    month_match = re.fullmatch(r"sale-month:([0-9]{4}-[0-9]{2})", selection)
    if month_match:
        return " AND SUBSTRING(auction_end_time FROM 1 FOR 7) = %s", [month_match.group(1)]

    date_match = re.fullmatch(r"sale-date:([0-9]{4}-[0-9]{2}-[0-9]{2})", selection)
    if date_match:
        return " AND SUBSTRING(auction_end_time FROM 1 FOR 10) = %s", [date_match.group(1)]

    lot_match = re.fullmatch(r"distribution:([A-Za-z0-9_.:-]{1,200})", selection)
    if lot_match:
        return " AND lot_id = %s", [lot_match.group(1)]

    return "", []


def _safe_view_name(view_name: str) -> str:
    """Allow only schema-qualified identifiers from trusted metadata."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?", view_name or ""):
        raise ValueError("Artifact data view name is not a safe identifier")
    return view_name


def _interactive_data_view_name(client_key: str, artifact_key: str, render_view_name: str) -> str:
    """Resolve an explicit interactive data view without changing render metadata."""
    return _safe_view_name(
        _INTERACTIVE_ARTIFACT_DATA_VIEWS.get((client_key, artifact_key), render_view_name)
    )


def _normalise_data_filters(filters: Optional[dict[str, Any]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in (filters or {}).items():
        if key not in _PRICING_FILTER_COLUMNS:
            continue
        text = str(value or "").strip()
        if text and text != "All":
            result[key] = text[:200]
    return result


def _filter_sql(filters: dict[str, str]) -> tuple[str, list[str]]:
    clauses = ["row_type = 'detail'"]
    params: list[str] = []
    for key, column in _PRICING_FILTER_COLUMNS.items():
        if key in filters:
            clauses.append(f"{column} = %s")
            params.append(filters[key])
    return " AND ".join(clauses), params


def _pricing_filter_options(data, view_name: str, selected_filters: dict[str, str]) -> dict[str, list[dict[str, Any]]]:
    """Return cascaded options, excluding each option's own active filter."""
    queries: list[str] = []
    params: list[str] = []
    for filter_key, expression in _PRICING_FILTER_COLUMNS.items():
        filters_without_current = {
            key: value for key, value in selected_filters.items() if key != filter_key
        }
        where_sql, where_params = _filter_sql(filters_without_current)
        value_sql = f"({expression})::text"
        queries.append(
            f"""
            SELECT '{filter_key}'::text AS filter_key,
                   {value_sql} AS filter_value,
                   COUNT(*)::bigint AS option_count
            FROM {view_name}
            WHERE {where_sql}
              AND NULLIF({value_sql}, '') IS NOT NULL
            GROUP BY {value_sql}
            """
        )
        params.extend(where_params)

    cursor = data.execute(" UNION ALL ".join(queries) + " ORDER BY filter_key, filter_value", params)
    options: dict[str, list[dict[str, Any]]] = {}
    for filter_key, filter_value, option_count in cursor.fetchall():
        options.setdefault(str(filter_key), []).append(
            {"value": str(filter_value), "count": int(option_count)}
        )
    return options


def _pricing_dashboard_summary(data, view_name: str, where_sql: str, params: list[str]) -> dict[str, Any]:
    """Calculate the complete selected dashboard state in PostgreSQL."""
    cursor = data.execute(
        f"""
        WITH selected AS (
            SELECT *,
                   {_PRICING_NUMERIC_PRICE} AS sale_price,
                   {_PRICING_NUMERIC_AGE} AS age_at_sale
            FROM {view_name}
            WHERE {where_sql}
        )
        SELECT
            COUNT(*)::bigint AS comparable_lot_count,
            COUNT(*) FILTER (WHERE sale_price > 0)::bigint AS sold_lot_count,
            COUNT(*) FILTER (WHERE demo_confidence = 'High')::bigint AS high_confidence_lot_count,
            COUNT(*) FILTER (WHERE demo_confidence = 'Medium')::bigint AS medium_confidence_lot_count,
            COUNT(*) FILTER (WHERE demo_confidence = 'Low')::bigint AS low_confidence_lot_count,
            MIN(sale_price) FILTER (WHERE sale_price > 0) AS min_sale_price,
            PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY sale_price)
                FILTER (WHERE sale_price > 0) AS median_sale_price,
            AVG(sale_price) FILTER (WHERE sale_price > 0) AS average_sale_price,
            MAX(sale_price) FILTER (WHERE sale_price > 0) AS max_sale_price,
            PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY age_at_sale)
                FILTER (WHERE sale_price > 0 AND age_at_sale IS NOT NULL) AS median_age_at_sale,
            PERCENTILE_CONT(0.5) WITHIN GROUP (
                ORDER BY CASE
                    WHEN NULLIF(item_usage_value, '') ~ '^[0-9]+(\\.[0-9]+)?$'
                        THEN NULLIF(item_usage_value, '')::numeric
                END
            ) FILTER (WHERE sale_price > 0) AS median_usage_value,
            MODE() WITHIN GROUP (ORDER BY NULLIF(item_usage_type, ''))
                FILTER (WHERE NULLIF(item_usage_type, '') IS NOT NULL) AS primary_usage_type,
            COUNT(*) FILTER (
                WHERE NULLIF(measurement, '') IS NOT NULL
                  AND measurement <> 'Not specified'
            )::bigint AS measurement_count,
            COUNT(*) FILTER (WHERE NULLIF(amount, '') IS NOT NULL)::bigint AS amount_count,
            COUNT(*) FILTER (
                WHERE NULLIF(group_name, '') IS NOT NULL
                  AND NULLIF(category, '') IS NOT NULL
                  AND category NOT IN ('Other / Mixed Lots', 'Unclassified')
            )::bigint AS type_subtype_count,
            COUNT(*) FILTER (WHERE NULLIF(item_year, '') IS NOT NULL)::bigint AS year_count,
            COUNT(*) FILTER (
                WHERE NULLIF(item_make, '') IS NOT NULL
                  AND NULLIF(item_model, '') IS NOT NULL
            )::bigint AS make_model_count,
            COUNT(*) FILTER (
                WHERE NULLIF(item_usage_value, '') IS NOT NULL
                  AND NULLIF(item_usage_type, '') IS NOT NULL
                  AND item_usage_type <> 'Unknown'
            )::bigint AS usage_count
        FROM selected
        """,
        params,
    )
    row = cursor.fetchone()
    columns = [description[0] for description in cursor.description]
    return dict(zip(columns, row))


def _pricing_chart_points(data, view_name: str, where_sql: str, params: list[str], *, vehicle_view: bool) -> dict[str, Any]:
    """Return aggregate chart points. No detail rows are sent for chart rendering."""
    common = f"""
        SELECT *,
               {_PRICING_NUMERIC_PRICE} AS sale_price,
               {_PRICING_NUMERIC_AGE} AS age_at_sale
        FROM {view_name}
        WHERE {where_sql}
    """
    if vehicle_view:
        cursor = data.execute(
            f"""
            WITH selected AS ({common})
            SELECT CONCAT('vehicle-age:', ROUND(age_at_sale)::integer) AS selection_key,
                   ROUND(age_at_sale)::double precision AS x,
                   ROUND(age_at_sale)::text AS label,
                   PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY sale_price) AS price,
                   MIN(sale_price) AS min,
                   MAX(sale_price) AS max,
                   COUNT(*)::bigint AS count
            FROM selected
            WHERE sale_price > 0 AND age_at_sale IS NOT NULL
            GROUP BY ROUND(age_at_sale)
            ORDER BY ROUND(age_at_sale)
            """,
            params,
        )
        return {"mode": "vehicle-age", "points": _rows_as_dicts(cursor)}

    month_cursor = data.execute(
        f"""
        WITH selected AS ({common}), sold AS (
            SELECT TO_DATE(SUBSTRING(auction_end_time FROM 1 FOR 7) || '-01', 'YYYY-MM-DD') AS sale_month,
                   sale_price
            FROM selected
            WHERE sale_price > 0
              AND auction_end_time ~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}'
        )
        SELECT CONCAT('sale-month:', TO_CHAR(sale_month, 'YYYY-MM')) AS selection_key,
               EXTRACT(EPOCH FROM sale_month) * 1000 AS x,
               TO_CHAR(sale_month, 'YY-MM') AS label,
               TO_CHAR(sale_month, 'MM') AS month_number,
               TO_CHAR(sale_month, 'YYYY') AS year_label,
               PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY sale_price) AS price,
               MIN(sale_price) AS min,
               MAX(sale_price) AS max,
               COUNT(*)::bigint AS count
        FROM sold
        GROUP BY sale_month
        ORDER BY sale_month
        """,
        params,
    )
    month_points = _rows_as_dicts(month_cursor)
    if len(month_points) >= 2:
        return {"mode": "sale-month", "points": month_points}

    date_cursor = data.execute(
        f"""
        WITH selected AS ({common}), sold AS (
            SELECT TO_DATE(SUBSTRING(auction_end_time FROM 1 FOR 10), 'YYYY-MM-DD') AS sale_date,
                   sale_price
            FROM selected
            WHERE sale_price > 0
              AND auction_end_time ~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}'
        )
        SELECT CONCAT('sale-date:', TO_CHAR(sale_date, 'YYYY-MM-DD')) AS selection_key,
               EXTRACT(EPOCH FROM sale_date) * 1000 AS x,
               TO_CHAR(sale_date, 'MM-DD') AS label,
               PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY sale_price) AS price,
               MIN(sale_price) AS min,
               MAX(sale_price) AS max,
               COUNT(*)::bigint AS count
        FROM sold
        GROUP BY sale_date
        ORDER BY sale_date
        """,
        params,
    )
    date_points = _rows_as_dicts(date_cursor)
    if len(date_points) >= 2:
        return {"mode": "sale-date", "points": date_points}

    distribution_cursor = data.execute(
        f"""
        WITH selected AS ({common}), sold AS (
            SELECT lot_id, sale_price
            FROM selected
            WHERE sale_price > 0
            ORDER BY sale_price, lot_id
            LIMIT 40
        )
        SELECT CONCAT('distribution:', lot_id) AS selection_key,
               ROW_NUMBER() OVER (ORDER BY sale_price, lot_id)::double precision AS x,
               CONCAT('Lot ', ROW_NUMBER() OVER (ORDER BY sale_price, lot_id)) AS label,
               sale_price AS price,
               sale_price AS min,
               sale_price AS max,
               1::bigint AS count
        FROM sold
        ORDER BY sale_price, lot_id
        """,
        params,
    )
    return {"mode": "distribution", "points": _rows_as_dicts(distribution_cursor)}


def fetch_artifact_data(
    client_key: str,
    artifact_key: str,
    *,
    filters: Optional[dict[str, Any]] = None,
    limit: int = 300,
    offset: int = 0,
    sort_key: str = "confidence",
    sort_direction: str = "asc",
    chart_selection: Optional[str] = None,
    authenticated_subject: Optional[str] = None,
) -> dict[str, Any]:
    """Return a complete server-calculated dashboard state and bounded table page."""
    limit = max(1, min(int(limit), 300))
    offset = max(0, int(offset))
    sort_columns = {
        "confidence": "CASE demo_confidence WHEN 'High' THEN 1 WHEN 'Medium' THEN 2 WHEN 'Low' THEN 3 ELSE 4 END",
        "date": "auction_end_time",
        "price": _PRICING_NUMERIC_PRICE,
        "age": _PRICING_NUMERIC_AGE,
        "usage": "CASE WHEN NULLIF(item_usage_value, '') ~ '^[0-9]+(\\.[0-9]+)?$' THEN NULLIF(item_usage_value, '')::numeric END",
        "type": "group_name",
        "subtype": "category",
        "auction": "COALESCE(NULLIF(auction_reference, ''), auction_title)",
        "lot": "lot_id",
    }
    order_expression = sort_columns.get(sort_key, sort_columns["confidence"])
    direction = "DESC" if str(sort_direction).lower() == "desc" else "ASC"
    selected_filters = _normalise_data_filters(filters)

    with get_metadata_conn() as meta:
        artifact = _fetch_artifact(meta, client_key=client_key, artifact_key=artifact_key)
        if artifact is None:
            raise ValueError(f"No active artifact found: client={client_key} artifact={artifact_key}")
        render_artifact = _resolve_render_artifact(meta, artifact)
        render_view_name = _safe_view_name(render_artifact["view_name"])
        view_name = _interactive_data_view_name(client_key, artifact_key, render_view_name)

    from .cache import get_artifact_cache, get_cache_settings

    cache_settings = get_cache_settings()
    with get_data_conn() as data:
        _set_authenticated_subject(data, authenticated_subject)
        freshness = _artifact_cache_freshness_timestamp(data, client_key, artifact_key)
        cache = get_artifact_cache(cache_settings) if cache_settings.enabled else None
        cache_params = {
            "cache_version": 1,
            "data_freshness_timestamp": freshness,
            "filters": selected_filters,
            "limit": limit,
            "offset": offset,
            "sort_key": sort_key,
            "sort_direction": direction,
            "chart_selection": chart_selection or "",
        }
        cache_key = cache.build_key(client_key, artifact_key, "data-page", cache_params) if cache else None
        if cache and cache_key:
            cached = cache.get(cache_key)
            if cached is not None:
                return cached

        where_sql, params = _filter_sql(selected_filters)
        chart_where_sql, chart_params = _pricing_chart_filter_sql(chart_selection)
        table_where_sql = where_sql + chart_where_sql
        table_params = [*params, *chart_params]
        detail_sql = f"""
            SELECT *
            FROM {view_name}
            WHERE {table_where_sql}
            ORDER BY {order_expression} {direction}, auction_end_time DESC, lot_id
            LIMIT %s OFFSET %s
        """
        detail_params = [*table_params, limit + 1, offset]
        detail_cursor = data.execute(detail_sql, detail_params)
        raw_rows = _rows_as_dicts(detail_cursor)
        has_more = len(raw_rows) > limit
        detail_rows = raw_rows[:limit]

        count_row = data.execute(
            f"SELECT COUNT(*) FROM {view_name} WHERE {table_where_sql}",
            table_params,
        ).fetchone()
        total_count = int(count_row[0])

        dashboard_summary = _pricing_dashboard_summary(data, view_name, where_sql, params)
        vehicle_view = selected_filters.get("type") == "Vehicles & Transport"
        chart = _pricing_chart_points(
            data,
            view_name,
            where_sql,
            params,
            vehicle_view=vehicle_view,
        )
        quality_cursor = data.execute(
            f"""
            SELECT *
            FROM {view_name}
            WHERE row_type = 'quality_metric'
            ORDER BY section_sort_order, support_sort_order, summary_detail_key
            """
        )
        quality_metrics = _rows_as_dicts(quality_cursor)
        filter_options = _pricing_filter_options(data, view_name, selected_filters)
        selected_parts = [value for value in selected_filters.values() if value]
        selected_label = " / ".join(selected_parts) if selected_parts else "All comparable groups"
        dashboard_summary.update(
            {
                "category_name": selected_label,
                "pricing_guidance_note": "Use the average as a cross-check against the median.",
                "demo_sample_warning": "All matching lots are reflected.",
                "confidence_guidance_note": "High-confidence lots have the required source-data signals for their type.",
            }
        )

        result = {
            "data_version": freshness,
            "rows": detail_rows,
            "dashboard": {
                "summary": dashboard_summary,
                "chart": chart,
                "quality_metrics": quality_metrics,
                "vehicle_view": vehicle_view,
            },
            "filter_options": filter_options,
            "total_count": total_count,
            "has_more": has_more,
            "next_offset": offset + len(detail_rows) if has_more else None,
        }
        # psycopg may return Decimal/date/UUID values; normalize before the
        # shared Redis JSON cache serializes the response.
        result = json.loads(json.dumps(result, default=str))
        if cache and cache_key:
            cache.set(cache_key, result, ttl_seconds=cache_settings.ttl_seconds)
        return result


def execute_artifact(
    client_key: str,
    artifact_key: str,
    behavior: str = "deliver",
    output_formats: Optional[list[str]] = None,
    refresh_cache: bool = False,
    authenticated_subject: Optional[str] = None,
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
    run_id: Optional[str] = str(uuid.uuid4())
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
                    _set_authenticated_subject(data, authenticated_subject)
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
                    _set_authenticated_subject(data, authenticated_subject)
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
                    run_id=run_id,
                    attachments=outputs,
                )

            # ── 7. Log the run and generated outputs ───────────────────
            completed_at = _now()
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
            "started_at", "completed_at", "error_message"]
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
    result["outputs"] = [dict(zip(output_keys, output)) for output in outputs]
    return result
