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
    def __init__(self, *, fail_on_execute=False, freshness_timestamp=None):
        self.calls = 0
        self.fail_on_execute = fail_on_execute
        self.freshness_timestamp = freshness_timestamp
        self.authenticated_subjects = []

    def execute(self, sql, params=None):
        if "set_config('bci.authenticated_subject'" in sql:
            self.authenticated_subjects.append(params[0])
            return FakeResult(row=("",))
        if "to_regprocedure('public.bci_artifact_cache_freshness(text,text)')" in sql:
            return FakeResult(row=(self.freshness_timestamp is not None,))
        if "public.bci_artifact_cache_freshness" in sql:
            return FakeResult(row=(self.freshness_timestamp,))

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
        freshness = params.get("data_freshness_timestamp") or "none"
        subject_hash = params.get("authenticated_subject_hash") or "anonymous"
        return (
            f"{client_key}:{artifact_key}:{cache_type}:"
            f"{params['behavior']}:{subject_hash}:{freshness}"
        )

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
    assert meta.log_params[7] == 2


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
    assert meta.log_params[7] == 4


def test_display_cache_key_includes_data_freshness_timestamp(monkeypatch):
    _, data = _patch_connections(
        monkeypatch,
        data=FakeData(freshness_timestamp="2026-07-07 10:00:00+00"),
    )
    fake_cache = FakeCache(payload=None)
    monkeypatch.setattr(
        cache_module,
        "get_cache_settings",
        lambda: cache_module.CacheSettings(enabled=True, ttl_seconds=42),
    )
    monkeypatch.setattr(cache_module, "get_artifact_cache", lambda settings: fake_cache)

    result = engine.execute_artifact("srp", "visit-counts", behavior="display")

    assert result["cache"]["data_freshness_timestamp"] == "2026-07-07 10:00:00+00"
    assert data.calls == 1
    assert fake_cache.set_calls[0][0] == (
        "srp:visit-counts:rendered:display:anonymous:2026-07-07 10:00:00+00"
    )


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
            "srp:visit-counts:rendered:display:anonymous:none",
            {"html": "<p>2</p>", "row_count": 2, "data_freshness_timestamp": None},
            42,
        )
    ]


def test_authenticated_subject_is_bound_to_data_query_and_cache(monkeypatch):
    _, data = _patch_connections(monkeypatch)
    fake_cache = FakeCache(payload=None)
    monkeypatch.setattr(
        cache_module,
        "get_cache_settings",
        lambda: cache_module.CacheSettings(enabled=True, ttl_seconds=42),
    )
    monkeypatch.setattr(cache_module, "get_artifact_cache", lambda settings: fake_cache)

    result = engine.execute_artifact(
        "srp",
        "visit-counts",
        behavior="display",
        authenticated_subject="user-1",
    )

    expected_hash = cache_module.hashlib.sha256(b"user-1").hexdigest()
    assert result["status"] == "success"
    assert data.authenticated_subjects == ["user-1", "user-1"]
    assert fake_cache.set_calls == [
        (
            f"srp:visit-counts:rendered:display:{expected_hash}:none",
            {"html": "<p>2</p>", "row_count": 2, "data_freshness_timestamp": None},
            42,
        )
    ]


def test_pricing_chart_selection_accepts_only_supported_server_filters():
    age_sql, age_params = engine._pricing_chart_filter_sql("vehicle-age:12")
    assert "ROUND" in age_sql
    assert age_params == ["12"]
    month_sql, month_params = engine._pricing_chart_filter_sql("sale-month:2026-08")
    assert month_sql == " AND SUBSTRING(auction_end_time FROM 1 FOR 7) = %s"
    assert month_params == ["2026-08"]
    assert engine._pricing_chart_filter_sql("not-a-supported-selection") == ("", [])


def test_pricing_interactive_contract_uses_the_full_detail_view():
    assert engine._interactive_data_view_name(
        "rag",
        "pricing-intelligence-overview",
        "public.rag_pricing_first_artifact_rows",
    ) == "public.rag_pricing_first_artifact_interactive_rows"


def test_pricing_data_page_returns_server_dashboard_and_bounded_rows(monkeypatch):
    class PricingData(FakeData):
        def __init__(self):
            super().__init__()
            self.detail_params = None

        def execute(self, sql, params=None):
            if "set_config('bci.authenticated_subject'" in sql:
                self.authenticated_subjects.append(params[0])
                return FakeResult(row=("",))
            if "to_regprocedure('public.bci_artifact_cache_freshness(text,text)')" in sql:
                return FakeResult(row=(False,))
            if "SELECT *" in sql and "LIMIT %s OFFSET %s" in sql:
                self.detail_params = params
                return FakeResult(
                    rows=[("detail", "lot-1")],
                    description=[("row_type",), ("lot_id",)],
                )
            if "SELECT COUNT(*)" in sql:
                return FakeResult(row=(1,))
            if "row_type = 'quality_metric'" in sql:
                return FakeResult(rows=[], description=[])
            raise AssertionError(f"Unexpected data query: {sql}")

    data = PricingData()
    _patch_connections(monkeypatch, data=data)
    monkeypatch.setattr(
        cache_module,
        "get_cache_settings",
        lambda: cache_module.CacheSettings(enabled=False),
    )
    monkeypatch.setattr(
        engine,
        "_pricing_dashboard_summary",
        lambda *_args: {"comparable_lot_count": 10, "sold_lot_count": 8},
    )
    monkeypatch.setattr(
        engine,
        "_pricing_chart_points",
        lambda *_args, **_kwargs: {"mode": "sale-month", "points": [{"selection_key": "sale-month:2026-08"}]},
    )
    monkeypatch.setattr(engine, "_pricing_filter_options", lambda *_args: {"type": [{"value": "Vehicles & Transport", "count": 10}]})

    result = engine.fetch_artifact_data(
        "rag",
        "pricing-intelligence-overview",
        filters={"type": "Vehicles & Transport"},
        chart_selection="vehicle-age:12",
    )

    assert result["dashboard"]["summary"]["comparable_lot_count"] == 10
    assert result["dashboard"]["chart"]["points"][0]["selection_key"] == "sale-month:2026-08"
    assert result["rows"] == [{"row_type": "detail", "lot_id": "lot-1"}]
    assert result["total_count"] == 1
    assert data.detail_params[-2:] == [301, 0]


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
