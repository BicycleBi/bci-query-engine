from contextlib import contextmanager

import pytest

from app import engine


class FakeResult:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class FakeMetadata:
    def execute(self, sql, params=None):
        if "to_regclass('app.artifact_assets')" in sql:
            return FakeResult(("app.artifact_assets",))
        if "FROM app.artifact_assets" in sql:
            assert params == ("rag", "lot-summary", "echarts.digest.min.js")
            return FakeResult(
                (
                    memoryview(b"window.echarts={};"),
                    "application/javascript",
                    "f" * 64,
                )
            )
        raise AssertionError(f"Unexpected metadata query: {sql}")


@contextmanager
def metadata_connection():
    yield FakeMetadata()


def test_get_artifact_asset_returns_exact_registry_content(monkeypatch):
    monkeypatch.setattr(engine, "get_metadata_conn", metadata_connection)

    result = engine.get_artifact_asset(
        "rag",
        "lot-summary",
        "echarts.digest.min.js",
    )

    assert result == {
        "content": b"window.echarts={};",
        "content_type": "application/javascript",
        "sha256": "f" * 64,
    }


@pytest.mark.parametrize(
    "asset_path",
    ["../secret", "nested/../../secret", "/absolute.js", "bad asset.js", ""],
)
def test_get_artifact_asset_rejects_unsafe_paths(asset_path):
    with pytest.raises(ValueError, match="invalid"):
        engine.get_artifact_asset("rag", "lot-summary", asset_path)
