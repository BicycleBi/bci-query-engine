import csv
import pytest
from app import outputs as engine


def test_csv_output_uses_database_columns_and_neutralizes_formulas(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTIFACT_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(engine, "_durable_mount", lambda root, mounts: True)
    contract = {
        "csv": {
            "filename": "filtered-fdhs.csv",
            "columns": [
                {"key": "fdh", "label": "FDH"},
                {"key": "active", "label": "Active Services"},
            ],
            "rows": [
                {"fdh": '=HYPERLINK("https://invalid.example")', "active": 12},
                {"fdh": "  +1", "active": 3},
                {"fdh": "FDH-2", "active": 0},
            ],
        }
    }

    output = engine._generate_csv_output(
        run_id="run-1",
        artifact_id="artifact-1",
        client_key="rf",
        artifact_key="market-penetration-fdh-csv-burst",
        query_result=contract,
    )

    with open(output["storage_path"], encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))

    assert rows == [
        ["FDH", "Active Services"],
        ['\'=HYPERLINK("https://invalid.example")', "12"],
        ["'  +1", "3"],
        ["FDH-2", "0"],
    ]
    assert output["output_format"] == "csv"
    assert output["output_role"] == "attachment"
    assert output["content_type"] == "text/csv; charset=utf-8"
    assert output["file_size_bytes"] > 0


def test_csv_output_requires_database_owned_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTIFACT_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(engine, "_durable_mount", lambda root, mounts: True)

    with pytest.raises(ValueError, match="database-owned csv contract"):
        engine._generate_csv_output(
            run_id="run-1",
            artifact_id="artifact-1",
            client_key="rf",
            artifact_key="market-penetration-fdh-csv-burst",
            query_result={"rows": []},
        )


def test_output_placement_requires_explicit_storage(monkeypatch):
    from pathlib import Path

    monkeypatch.delenv("ARTIFACT_OUTPUT_DIR", raising=False)
    with pytest.raises(ValueError, match="ARTIFACT_OUTPUT_DIR"):
        engine._artifact_output_dir()
    mounts = (
        "1 0 0:1 / / rw - overlay overlay rw\n"
        "2 1 1:2 /exports /reports rw - ext4 /dev/test rw\n"
        "3 2 0:3 / /reports/temp rw - tmpfs tmpfs rw"
    )
    assert engine._durable_mount(Path("/reports/exports"), mounts)
    assert not engine._durable_mount(Path("/reports/temp/exports"), mounts)
    assert not engine._durable_mount(Path("/tmp/outputs"), mounts)
