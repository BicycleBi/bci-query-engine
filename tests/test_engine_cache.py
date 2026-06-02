import importlib
import sys
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

sys.modules.setdefault("psycopg", SimpleNamespace(connect=None))
sys.modules.setdefault("requests", SimpleNamespace(post=None))

engine = importlib.import_module("app.engine")
cache_module = importlib.import_module("app.cache")


ARTIFACT_ID = "11111111-1111-1111-1111-111111111111"
TEMPLATE_ID = "22222222-2222-2222-2222-222222222222"


class FakeResult:
    def __init__(self, *, row=None, rows=None, description=None):
        self._row = row
        self._rows = rows or []
        self.description = description or []

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


class FakeMeta:
    def __init__(self):
        self.log_params = None
        self.commits = 0

    def execute(self, sql, params=None):
        if "FROM app.artifacts a" in sql:
            return FakeResult(
                row=(
                    ARTIFACT_ID,
                    "srp",
                    "visit-counts",
                    "rpt.visit_counts",
                    "web",
                    "Visit Counts",
                    "<p>{{ rows|length }}</p>",
                    TEMPLATE_ID,
                )
            )

        if "INSERT INTO log.artifact_runs" in sql:
            self.log_params = params
            return FakeResult(row=("run-1",))

        raise AssertionError(f"Unexpected metadata query: {sql}")

    def commit(self):
        self.commits += 1


class FakeData:
    def __init__(self, *, fail_on_execute=False):
        self.calls = 0
        self.fail_on_execute = fail_on_execute

    def execute(self, sql):
        self.calls += 1
        if self.fail_on_execute:
            raise AssertionError("Data DB should not be queried on cache hit")
        return FakeResult(
            rows=[("Mon",), ("Tue",)],
            description=[("day",)],
        )


class FakeCache:
    def __init__(self, payload=None, *, fail_get=False):
        self.payload = payload
        self.fail_get = fail_get
        self.set_calls = []

    @staticmethod
    def build_key(client_key, artifact_key, cache_type, params):
        return f"{client_key}:{artifact_key}:{cache_type}:{params['behavior']}"

    def get(self, key):
        if self.fail_get:
            raise RuntimeError("redis down")
        return self.payload

    def set(self, key, value, ttl_seconds):
        self.set_calls.append((key, value, ttl_seconds))


@contextmanager
def _yield(value):
    yield value


def _patch_connections(monkeypatch, *, data=None):
    meta = FakeMeta()
    data = data or FakeData()
    monkeypatch.setattr(engine, "get_metadata_conn", lambda: _yield(meta))
    monkeypatch.setattr(engine, "get_data_conn", lambda: _yield(data))
    return meta, data


def test_redis_disabled_preserves_display_execution(monkeypatch):
    meta, data = _patch_connections(monkeypatch)
    monkeypatch.setattr(
        cache_module,
        "get_cache_settings",
        lambda: cache_module.CacheSettings(enabled=False),
    )
    monkeypatch.setattr(
        cache_module,
        "get_artifact_cache",
        lambda settings: pytest.fail("Redis cache should not be created when disabled"),
    )

    result = engine.execute_artifact("srp", "visit-counts", behavior="display")

    assert result["status"] == "success"
    assert result["preview_html"] == "<p>2</p>"
    assert data.calls == 1
    assert meta.log_params[6] == 2


def test_display_cache_hit_skips_data_query(monkeypatch):
    meta, data = _patch_connections(monkeypatch, data=FakeData(fail_on_execute=True))
    fake_cache = FakeCache(payload={"html": "<p>cached</p>", "row_count": 4})
    monkeypatch.setattr(
        cache_module,
        "get_cache_settings",
        lambda: cache_module.CacheSettings(enabled=True),
    )
    monkeypatch.setattr(cache_module, "get_artifact_cache", lambda settings: fake_cache)

    result = engine.execute_artifact("srp", "visit-counts", behavior="display")

    assert result["status"] == "success"
    assert result["preview_html"] == "<p>cached</p>"
    assert data.calls == 0
    assert meta.log_params[6] == 4


def test_display_cache_miss_queries_and_sets_render(monkeypatch):
    _, data = _patch_connections(monkeypatch)
    fake_cache = FakeCache(payload=None)
    monkeypatch.setattr(
        cache_module,
        "get_cache_settings",
        lambda: cache_module.CacheSettings(enabled=True, ttl_seconds=42),
    )
    monkeypatch.setattr(cache_module, "get_artifact_cache", lambda settings: fake_cache)

    result = engine.execute_artifact("srp", "visit-counts", behavior="display")

    assert result["preview_html"] == "<p>2</p>"
    assert data.calls == 1
    assert fake_cache.set_calls == [
        (
            "srp:visit-counts:rendered:display",
            {"html": "<p>2</p>", "row_count": 2},
            42,
        )
    ]


def test_redis_read_failure_falls_back_to_normal_execution(monkeypatch):
    _, data = _patch_connections(monkeypatch)
    fake_cache = FakeCache(fail_get=True)
    monkeypatch.setattr(
        cache_module,
        "get_cache_settings",
        lambda: cache_module.CacheSettings(enabled=True),
    )
    monkeypatch.setattr(cache_module, "get_artifact_cache", lambda settings: fake_cache)

    result = engine.execute_artifact("srp", "visit-counts", behavior="display")

    assert result["status"] == "success"
    assert result["preview_html"] == "<p>2</p>"
    assert data.calls == 1


def test_delivery_execution_does_not_use_cache(monkeypatch):
    _, data = _patch_connections(monkeypatch)
    monkeypatch.setattr(
        cache_module,
        "get_cache_settings",
        lambda: cache_module.CacheSettings(enabled=True),
    )
    monkeypatch.setattr(
        cache_module,
        "get_artifact_cache",
        lambda settings: pytest.fail("Delivery executions should not create Redis cache"),
    )

    result = engine.execute_artifact("srp", "visit-counts", behavior="deliver")

    assert result["status"] == "success"
    assert result["preview_html"] is None
    assert data.calls == 1
