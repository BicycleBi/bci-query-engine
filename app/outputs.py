"""Generate declared report files in stack-provisioned output storage."""

import csv
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from datetime import datetime
from typing import Any


def _safe_filename_part(value: Any, fallback: str = "artifact") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9 ._-]+", "", str(value or fallback)).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:120] or fallback


def _row_slice_label(row: dict[str, Any], fallback: str) -> str:
    for key in ("slice_label", "slice_name", "slice_key"):
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


def _durable_mount(root: Path, mountinfo: str) -> bool:
    # Linux mountinfo includes bind mounts even when st_dev matches the parent.
    # The deepest covering mount determines the actual backing filesystem.
    covering = []
    for line in mountinfo.splitlines():
        fields, filesystem = line.split(" - ", 1)
        point = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), fields.split()[4])
        mount = Path(point)
        if root.is_relative_to(mount):
            covering.append((len(mount.parts), mount, filesystem.split()[0]))
    if not covering:
        return False
    _, mount, kind = max(covering, key=lambda entry: entry[0])
    return mount != Path("/") and kind not in {"tmpfs", "ramfs", "overlay"}


def _artifact_output_dir() -> Path:
    configured = os.getenv("ARTIFACT_OUTPUT_DIR")
    if not configured or not Path(configured).is_absolute() or not Path(configured).is_dir():
        raise ValueError("File output requires an existing mounted ARTIFACT_OUTPUT_DIR")
    root = Path(configured).resolve(strict=True)
    mountinfo = Path("/proc/self/mountinfo")
    if mountinfo.exists() and not _durable_mount(root, mountinfo.read_text()):
        raise ValueError("ARTIFACT_OUTPUT_DIR must use persistent mounted storage")
    return root


def create_output_directory(client_key: str, artifact_key: str, run_id: str) -> Path:
    root = _artifact_output_dir()
    directory = root / client_key / artifact_key / run_id
    if not directory.resolve().is_relative_to(root):
        raise ValueError("Invalid output directory")
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    return directory


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
        slice_key = row.get("slice_key") or slice_label
        filename = (
            f"{_safe_filename_part(subject or artifact_key)} - "
            f"{_safe_filename_part(slice_label, fallback=f'slice-{index}')} - "
            f"{render_date}.pdf"
        )
        pdf_path = output_root / filename
        if pdf_path.exists():
            raise ValueError("Duplicate PDF slice filename")
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


def _csv_cell(value: Any) -> Any:
    """Return a spreadsheet-safe scalar while preserving numeric values."""
    if value is None:
        return ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    if isinstance(value, (dict, list)):
        value = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    text = str(value)
    if text.startswith(("\t", "\r", "\n")) or text.lstrip().startswith(("=", "+", "-", "@")):
        return f"\x27{text}"
    return text


def _generate_csv_output(
    *,
    run_id: str,
    artifact_id: str,
    client_key: str,
    artifact_key: str,
    query_result: Any,
) -> dict[str, Any]:
    """Generate one CSV from a database-owned ``csv`` output contract."""
    if not isinstance(query_result, dict) or not isinstance(query_result.get("csv"), dict):
        raise ValueError("CSV output query must return a database-owned csv contract")

    contract = query_result["csv"]
    columns = contract.get("columns")
    rows = contract.get("rows")
    if not isinstance(columns, list) or not columns or len(columns) > 64:
        raise ValueError("CSV output contract must define between 1 and 64 columns")
    if not isinstance(rows, list):
        raise ValueError("CSV output contract rows must be an array")

    max_rows = max(1, int(os.getenv("CSV_OUTPUT_MAX_ROWS", "100000")))
    if len(rows) > max_rows:
        raise ValueError(f"CSV output exceeds the {max_rows}-row limit")

    normalized_columns: list[tuple[str, str]] = []
    for column in columns:
        if not isinstance(column, dict):
            raise ValueError("CSV output columns must be objects")
        key = str(column.get("key") or "").strip()
        label = str(column.get("label") or "").strip()
        if not key or not label or len(key) > 100 or len(label) > 200:
            raise ValueError("CSV output columns require bounded key and label values")
        normalized_columns.append((key, label))

    if any(not isinstance(row, dict) for row in rows):
        raise ValueError("CSV output rows must be objects")

    requested_filename = str(contract.get("filename") or f"{artifact_key}.csv")
    filename = f"{_safe_filename_part(Path(requested_filename).stem)}.csv"
    output_root = _artifact_output_dir() / client_key / artifact_key / run_id
    output_root.mkdir(parents=True, exist_ok=True)
    output_path = output_root / filename

    with output_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow([label for _, label in normalized_columns])
        for row in rows:
            writer.writerow([_csv_cell(row.get(key)) for key, _ in normalized_columns])

    file_size = output_path.stat().st_size
    max_bytes = max(1, int(os.getenv("CSV_OUTPUT_MAX_BYTES", str(3 * 1024 * 1024))))
    if file_size > max_bytes:
        output_path.unlink(missing_ok=True)
        raise ValueError(f"CSV output exceeds the {max_bytes}-byte attachment limit")

    return {
        "run_id": run_id,
        "artifact_id": artifact_id,
        "artifact_key": artifact_key,
        "client_key": client_key,
        "output_format": "csv",
        "output_role": "attachment",
        "slice_key": None,
        "slice_label": None,
        "filename": filename,
        "storage_path": str(output_path),
        "content_type": "text/csv; charset=utf-8",
        "file_size_bytes": file_size,
        "sha256": _sha256_file(output_path),
        "status": "completed",
    }


def _insert_artifact_outputs(meta, outputs: list[dict[str, Any]]) -> None:
    if not outputs:
        return
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
