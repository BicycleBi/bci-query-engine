import importlib
import json
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

sys.modules.setdefault("psycopg", SimpleNamespace(connect=None, Connection=object))
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

        if "FROM app.artifact_delivery_targets" in sql:
            return FakeResult(row=(False,))

        raise AssertionError(f"Unexpected metadata query: {sql}")

    def commit(self):
        self.commits += 1


class FakeData:
    def __init__(self, *, fail_on_execute=False, freshness_timestamp=None):
        self.calls = 0
        self.fail_on_execute = fail_on_execute
        self.freshness_timestamp = freshness_timestamp
        self.authenticated_subjects = []
        self.authorized_roles = []
        self.platform_admin_roles = []
        self.platform_corporate_roles = []

    def execute(self, sql, params=None):
        if "set_config('bci.authenticated_subject'" in sql:
            self.authenticated_subjects.append(params[0])
            return FakeResult(row=("",))
        if "set_config('bci.authorized_roles'" in sql:
            self.authorized_roles.append(params[0])
            return FakeResult(row=("",))
        if "set_config('bci.platform_admin_role'" in sql:
            self.platform_admin_roles.append(params[0])
            return FakeResult(row=("",))
        if "set_config('bci.platform_corporate_role'" in sql:
            self.platform_corporate_roles.append(params[0])
            return FakeResult(row=("",))
        if "to_regprocedure('public.bci_artifact_cache_freshness(text,text)')" in sql:
            return FakeResult(row=(self.freshness_timestamp is not None,))
        if "public.bci_artifact_cache_freshness" in sql:
            return FakeResult(row=(self.freshness_timestamp,))
        if "SELECT to_regprocedure(%s)" in sql and str(params[0]).endswith("_query_cache_policy()"):
            return FakeResult(row=(None,))

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
        self.invalidated_patterns = []
        self.generation = None
        self.activated_generations = []

    @staticmethod
    def build_key(client_key, artifact_key, cache_type, params):
        freshness = params.get("data_freshness_timestamp") or "none"
        subject_hash = params.get("authenticated_subject_hash") or "anonymous"
        authorization_context_hash = params.get("authorization_context_hash")
        authorization_suffix = (
            f":{authorization_context_hash}" if authorization_context_hash else ""
        )
        discriminator = params.get("behavior") or json.dumps(
            params.get("query", {}),
            separators=(",", ":"),
            sort_keys=True,
        )
        return (
            f"{client_key}:{artifact_key}:{cache_type}:"
            f"{discriminator}:{subject_hash}{authorization_suffix}:{freshness}"
        )

    def get(self, key):
        if self.fail_get:
            raise RuntimeError("redis down")
        return self.payload

    def set(self, key, value, ttl_seconds):
        self.set_calls.append((key, value, ttl_seconds))

    def invalidate_pattern(self, pattern):
        self.invalidated_patterns.append(pattern)
        return 0

    def get_query_generation(self, client_key, artifact_key):
        return self.generation

    def activate_query_generation(self, client_key, artifact_key, generation):
        previous = self.generation
        self.generation = generation
        self.activated_generations.append(generation)
        return previous


@contextmanager
def _yield(value):
    yield value


def _patch_connections(monkeypatch, *, data=None):
    meta = FakeMeta()
    data = data or FakeData()
    monkeypatch.setattr(engine, "get_metadata_conn", lambda: _yield(meta))
    monkeypatch.setattr(engine, "get_data_conn", lambda: _yield(data))
    return meta, data


def test_queue_artifact_execution_persists_immediate_status(monkeypatch):
    meta, _ = _patch_connections(monkeypatch)

    result = engine.queue_artifact_execution("srp", "visit-counts")

    assert result["status"] == "queued"
    assert result["client_key"] == "srp"
    assert result["artifact_key"] == "visit-counts"
    assert result["completed_at"] is None
    assert meta.log_params[1] == ARTIFACT_ID
    assert meta.log_params[5] == "queued"
    assert meta.log_params[6] == "web"
    assert meta.commits == 1


def test_cancel_queue_is_exact_and_terminal(monkeypatch):
    class CancelMeta:
        def __init__(self):
            self.commits = 0
            self.update_params = None

        def execute(self, sql, params=None):
            if "SELECT count(*)" in sql:
                return FakeResult(row=(2,))
            if "UPDATE log.artifact_runs" in sql:
                self.update_params = params
                return FakeResult(rows=[("run-1",), ("run-2",)])
            raise AssertionError(f"Unexpected metadata query: {sql}")

        def commit(self):
            self.commits += 1

    meta = CancelMeta()
    monkeypatch.setattr(engine, "get_metadata_conn", lambda: _yield(meta))

    result = engine.cancel_queued_artifact_executions(
        "srp",
        [
            "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222222222",
        ],
        reason="Verified synchronous recovery delivered these reports",
    )

    assert result == {"status": "cancelled", "cancelled_count": 2}
    assert meta.commits == 1
    assert meta.update_params[1] == "srp"


def test_cancel_queue_fails_closed_on_partial_match(monkeypatch):
    class PartialCancelMeta:
        def execute(self, sql, params=None):
            if "SELECT count(*)" in sql:
                return FakeResult(row=(1,))
            raise AssertionError("Update must not run after a partial match")

        def commit(self):
            raise AssertionError("Partial match must not commit")

    monkeypatch.setattr(engine, "get_metadata_conn", lambda: _yield(PartialCancelMeta()))

    with pytest.raises(ValueError, match="target mismatch"):
        engine.cancel_queued_artifact_executions(
            "srp",
            [
                "11111111-1111-1111-1111-111111111111",
                "22222222-2222-2222-2222-222222222222",
            ],
            reason="Verified synchronous recovery delivered these reports",
        )


def test_get_run_normalizes_database_uuid_for_api_contract(monkeypatch):
    run_id = UUID("33333333-3333-3333-3333-333333333333")
    started_at = datetime.now(tz=timezone.utc)

    class FakeRunMeta:
        def execute(self, sql, params=None):
            if "FROM log.artifact_runs r" in sql:
                return FakeResult(
                    row=(
                        run_id,
                        "srp",
                        "visit-counts",
                        "completed",
                        started_at,
                        started_at,
                        None,
                    )
                )
            if "FROM log.artifact_outputs" in sql:
                return FakeResult(rows=[])
            if "CREATE TABLE IF NOT EXISTS log.artifact_outputs" in sql:
                return FakeResult()
            if "CREATE INDEX IF NOT EXISTS artifact_outputs_" in sql:
                return FakeResult()
            raise AssertionError(f"Unexpected metadata query: {sql}")

    monkeypatch.setattr(engine, "get_metadata_conn", lambda: _yield(FakeRunMeta()))

    result = engine.get_run(str(run_id))

    assert result is not None
    assert result["run_id"] == str(run_id)
    assert isinstance(result["run_id"], str)


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
        authorized_roles=["srp_pnl_scope_b", "srp_pnl_scope_a"],
    )

    expected_hash = cache_module.hashlib.sha256(b"user-1").hexdigest()
    expected_scope_hash = cache_module.hashlib.sha256(
        b'["srp_pnl_scope_a","srp_pnl_scope_b"]'
    ).hexdigest()
    assert result["status"] == "success"
    assert data.authenticated_subjects == ["user-1", "user-1"]
    assert data.authorized_roles == [
        '["srp_pnl_scope_a","srp_pnl_scope_b"]',
        '["srp_pnl_scope_a","srp_pnl_scope_b"]',
    ]
    assert fake_cache.set_calls == [
        (
            f"srp:visit-counts:rendered:display:{expected_hash}:{expected_scope_hash}:none",
            {"html": "<p>2</p>", "row_count": 2, "data_freshness_timestamp": None},
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


def test_query_function_name_follows_the_render_view_contract():
    assert engine._query_function_name(
        "public.rag_pricing_first_artifact_rows"
    ) == "public.rag_pricing_first_artifact_rows_query"


class FakeQueryData(FakeData):
    def __init__(self, *, contract_exists=True, cache_scope=None, cache_policy=None, result=None, **kwargs):
        super().__init__(**kwargs)
        self.contract_exists = contract_exists
        self.cache_scope = cache_scope
        self.cache_policy = cache_policy
        self.result = result if result is not None else {"rows": [], "total_count": 0}
        self.query_payloads = []

    def execute(self, sql, params=None):
        if "set_config('bci.authenticated_subject'" in sql:
            self.authenticated_subjects.append(params[0])
            return FakeResult(row=("",))
        if "set_config('bci.authorized_roles'" in sql:
            self.authorized_roles.append(params[0])
            return FakeResult(row=("",))
        if "set_config('bci.platform_admin_role'" in sql:
            self.platform_admin_roles.append(params[0])
            return FakeResult(row=("",))
        if "set_config('bci.platform_corporate_role'" in sql:
            self.platform_corporate_roles.append(params[0])
            return FakeResult(row=("",))
        if "to_regprocedure('public.bci_artifact_cache_freshness(text,text)')" in sql:
            return FakeResult(row=(self.freshness_timestamp is not None,))
        if "public.bci_artifact_cache_freshness" in sql:
            return FakeResult(row=(self.freshness_timestamp,))
        if "SELECT to_regprocedure(%s)" in sql:
            if str(params[0]).endswith("_query_cache_policy()"):
                return FakeResult(
                    row=("rpt.visit_counts_query_cache_policy()",)
                    if self.cache_policy is not None
                    else (None,)
                )
            if str(params[0]).endswith("_query_cache_scope(jsonb)"):
                return FakeResult(
                    row=("rpt.visit_counts_query_cache_scope(jsonb)",)
                    if self.cache_scope is not None
                    else (None,)
                )
            return FakeResult(row=("rpt.visit_counts_query(jsonb)",) if self.contract_exists else (None,))
        if "SELECT rpt.visit_counts_query_cache_scope" in sql:
            return FakeResult(row=(self.cache_scope,))
        if "SELECT rpt.visit_counts_query_cache_policy" in sql:
            return FakeResult(row=(self.cache_policy,))
        if "SELECT rpt.visit_counts_query" in sql:
            self.calls += 1
            self.query_payloads.append(params[0])
            return FakeResult(row=(self.result,))
        raise AssertionError(f"Unexpected data query: {sql}")


def test_artifact_query_passes_opaque_json_to_database(monkeypatch):
    data = FakeQueryData(result={"dashboard": {"summary": {"total": 7}}})
    _patch_connections(monkeypatch, data=data)
    monkeypatch.setattr(
        cache_module,
        "get_cache_settings",
        lambda: cache_module.CacheSettings(enabled=False),
    )

    query = {
        "operation": "summary",
        "filters": {"type": "Vehicles & Transport"},
        "pagination": {"limit": 100, "offset": 0},
    }
    result = engine.execute_artifact_query(
        "srp",
        "visit-counts",
        query=query,
        authenticated_subject="user-1",
        authorized_roles=["scope-a"],
    )

    assert result == {"dashboard": {"summary": {"total": 7}}}
    assert json.loads(data.query_payloads[0]) == query
    assert data.authenticated_subjects == ["user-1"]
    assert data.authorized_roles == ['["scope-a"]']


def test_artifact_query_cache_hit_skips_database_contract(monkeypatch):
    data = FakeQueryData(fail_on_execute=True, freshness_timestamp="2026-08-04 10:00:00+00")
    _patch_connections(monkeypatch, data=data)
    fake_cache = FakeCache(payload={"cached": True})
    monkeypatch.setattr(
        cache_module,
        "get_cache_settings",
        lambda: cache_module.CacheSettings(enabled=True),
    )
    monkeypatch.setattr(cache_module, "get_artifact_cache", lambda settings: fake_cache)

    result = engine.execute_artifact_query(
        "srp",
        "visit-counts",
        query={"operation": "summary"},
    )

    assert result == {"cached": True}
    assert data.calls == 0


def test_shared_artifact_query_cache_key_is_not_identity_scoped(monkeypatch):
    data = FakeQueryData(cache_scope="shared", freshness_timestamp="2026-08-12 10:00:00+00")
    _patch_connections(monkeypatch, data=data)
    fake_cache = FakeCache(payload=None)
    monkeypatch.setattr(
        cache_module,
        "get_cache_settings",
        lambda: cache_module.CacheSettings(enabled=True, ttl_seconds=42),
    )
    monkeypatch.setattr(cache_module, "get_artifact_cache", lambda settings: fake_cache)

    engine.execute_artifact_query(
        "rag",
        "lot-summary",
        query={"operation": "dataset", "dataset": "summary"},
        authenticated_subject="user-1",
        authorized_roles=["scope-a"],
    )

    cache_key = fake_cache.set_calls[0][0]
    expected_user_hash = cache_module.hashlib.sha256(b"user-1").hexdigest()
    assert expected_user_hash not in cache_key
    assert fake_cache.set_calls[0][2] == 42


def test_prewarm_builds_declared_shared_query_entries(monkeypatch):
    data = FakeQueryData(
        cache_scope="shared",
        freshness_timestamp="2026-08-12 10:00:00+00",
        result={"dataset": "summary", "rows": []},
    )
    _patch_connections(monkeypatch, data=data)
    fake_cache = FakeCache(payload=None)
    monkeypatch.setattr(
        cache_module,
        "get_cache_settings",
        lambda: cache_module.CacheSettings(enabled=True, ttl_seconds=42),
    )
    monkeypatch.setattr(cache_module, "get_artifact_cache", lambda settings: fake_cache)

    result = engine.prewarm_artifact_query_cache(
        "rag",
        "lot-summary",
        queries=[{"operation": "dataset", "dataset": "summary"}],
    )

    assert result == {
        "client_key": "rag",
        "artifact_key": "lot-summary",
        "status": "prewarmed",
        "entry_count": 1,
        "refresh_mode": "ttl",
    }
    assert fake_cache.invalidated_patterns == [
        "bci:cache:rag:lot-summary:artifact-query:*"
    ]
    assert len(fake_cache.set_calls) == 1
    prewarmed_key = fake_cache.set_calls[0][0]

    engine.execute_artifact_query(
        "rag",
        "lot-summary",
        query={"operation": "dataset", "dataset": "summary"},
        authenticated_subject="user-2",
        authorized_roles=["different-role"],
    )

    assert fake_cache.set_calls[1][0] == prewarmed_key


def test_prewarm_swap_activates_complete_persistent_generation(monkeypatch):
    data = FakeQueryData(
        cache_scope="shared",
        cache_policy={"refresh_mode": "prewarm_swap", "ttl_seconds": 0},
        result={"rows": []},
    )
    _patch_connections(monkeypatch, data=data)
    fake_cache = FakeCache(payload=None)
    fake_cache.generation = "prior-generation"
    monkeypatch.setattr(
        cache_module,
        "get_cache_settings",
        lambda: cache_module.CacheSettings(enabled=True, ttl_seconds=42),
    )
    monkeypatch.setattr(cache_module, "get_artifact_cache", lambda settings: fake_cache)

    result = engine.prewarm_artifact_query_cache(
        "srp",
        "practice-performance-pulse",
        queries=[
            {"operation": "appointments"},
            {"operation": "claims"},
            {"operation": "ewc"},
        ],
    )

    assert result["refresh_mode"] == "prewarm_swap"
    assert result["entry_count"] == 3
    assert len(fake_cache.activated_generations) == 1
    active = fake_cache.activated_generations[0]
    assert active != "prior-generation"
    assert all(f"artifact-query-generation:{active}:" in call[0] for call in fake_cache.set_calls)
    assert all(call[2] is None for call in fake_cache.set_calls)
    assert fake_cache.invalidated_patterns == [
        "bci:cache:srp:practice-performance-pulse:artifact-query-generation:prior-generation:*"
    ]


def test_prewarm_swap_failure_keeps_prior_generation_active(monkeypatch):
    class FailingCache(FakeCache):
        def set(self, key, value, ttl_seconds):
            super().set(key, value, ttl_seconds)
            raise RuntimeError("redis write failed")

    data = FakeQueryData(
        cache_scope="shared",
        cache_policy={"refresh_mode": "prewarm_swap", "ttl_seconds": 0},
    )
    _patch_connections(monkeypatch, data=data)
    fake_cache = FailingCache(payload=None)
    fake_cache.generation = "last-good"
    monkeypatch.setattr(
        cache_module,
        "get_cache_settings",
        lambda: cache_module.CacheSettings(enabled=True),
    )
    monkeypatch.setattr(cache_module, "get_artifact_cache", lambda settings: fake_cache)

    with pytest.raises(RuntimeError, match="redis write failed"):
        engine.prewarm_artifact_query_cache(
            "srp",
            "practice-performance-pulse",
            queries=[{"operation": "appointments"}],
        )

    assert fake_cache.generation == "last-good"
    assert fake_cache.activated_generations == []
    assert len(fake_cache.invalidated_patterns) == 1
    assert "artifact-query-generation:" in fake_cache.invalidated_patterns[0]


def test_prewarm_swap_activation_uncertainty_keeps_complete_candidate(monkeypatch):
    class ActivationFailureCache(FakeCache):
        def activate_query_generation(self, client_key, artifact_key, generation):
            raise RuntimeError("redis activation response lost")

    data = FakeQueryData(
        cache_scope="shared",
        cache_policy={"refresh_mode": "prewarm_swap", "ttl_seconds": 0},
    )
    _patch_connections(monkeypatch, data=data)
    fake_cache = ActivationFailureCache(payload=None)
    fake_cache.generation = "last-good"
    monkeypatch.setattr(
        cache_module,
        "get_cache_settings",
        lambda: cache_module.CacheSettings(enabled=True),
    )
    monkeypatch.setattr(cache_module, "get_artifact_cache", lambda settings: fake_cache)

    with pytest.raises(RuntimeError, match="activation response lost"):
        engine.prewarm_artifact_query_cache(
            "srp",
            "practice-performance-pulse",
            queries=[{"operation": "appointments"}],
        )

    assert len(fake_cache.set_calls) == 1
    assert fake_cache.generation == "last-good"
    assert fake_cache.invalidated_patterns == []


def test_prewarm_swap_query_uses_active_generation_and_no_expiry(monkeypatch):
    data = FakeQueryData(
        cache_scope="shared",
        cache_policy={"refresh_mode": "prewarm_swap", "ttl_seconds": 0},
    )
    _patch_connections(monkeypatch, data=data)
    fake_cache = FakeCache(payload=None)
    fake_cache.generation = "active-generation"
    monkeypatch.setattr(
        cache_module,
        "get_cache_settings",
        lambda: cache_module.CacheSettings(enabled=True, ttl_seconds=42),
    )
    monkeypatch.setattr(cache_module, "get_artifact_cache", lambda settings: fake_cache)

    engine.execute_artifact_query(
        "srp",
        "practice-performance-pulse",
        query={"operation": "appointments"},
    )

    assert "artifact-query-generation:active-generation:" in fake_cache.set_calls[0][0]
    assert fake_cache.set_calls[0][2] is None


def test_prewarm_rejects_identity_scoped_query(monkeypatch):
    data = FakeQueryData(cache_scope="identity")
    _patch_connections(monkeypatch, data=data)
    fake_cache = FakeCache(payload=None)
    monkeypatch.setattr(
        cache_module,
        "get_cache_settings",
        lambda: cache_module.CacheSettings(enabled=True),
    )
    monkeypatch.setattr(cache_module, "get_artifact_cache", lambda settings: fake_cache)

    with pytest.raises(ValueError, match="only shared query contracts"):
        engine.prewarm_artifact_query_cache(
            "rag",
            "lot-summary",
            queries=[{"operation": "dataset", "dataset": "summary"}],
        )


def test_artifact_query_requires_database_contract(monkeypatch):
    data = FakeQueryData(contract_exists=False)
    _patch_connections(monkeypatch, data=data)
    monkeypatch.setattr(
        cache_module,
        "get_cache_settings",
        lambda: cache_module.CacheSettings(enabled=False),
    )

    with pytest.raises(ValueError, match="no database query contract"):
        engine.execute_artifact_query(
            "srp",
            "visit-counts",
            query={"operation": "summary"},
        )

def test_server_config_binds_platform_admin_role_to_transaction(monkeypatch):
    monkeypatch.setenv("SRP_BICYCLE_ADMIN_ROLE", "srpqa_admin")
    monkeypatch.setenv("SRP_CORPORATE_ANALYTICS_ROLE", "uat")
    data = FakeData()
    engine._set_authorization_context(data, "srp", "fixture-subject", ["srpqa_admin"])
    assert data.platform_admin_roles == ["srpqa_admin"]
    assert data.platform_corporate_roles == ["uat"]
    assert data.authorized_roles == ['["srpqa_admin"]']


def test_generic_analytics_roles_bind_to_transaction_without_srp_names(monkeypatch):
    monkeypatch.setenv("ANALYTICS_REPORTING_CLIENT_KEY", "rf")
    monkeypatch.setenv("ANALYTICS_BICYCLE_ADMIN_ROLE", "rfdev_admin")
    monkeypatch.setenv("ANALYTICS_CLIENT_REPORTING_ROLE", "rfdev_analytics")
    data = FakeData()
    engine._set_authorization_context(data, "rf", "fixture-subject", ["rfdev_admin"])
    assert data.platform_admin_roles == ["rfdev_admin"]
    assert data.platform_corporate_roles == ["rfdev_analytics"]
    assert data.authorized_roles == ['["rfdev_admin"]']


@pytest.mark.parametrize("outcome", ["current", "wrong-request", "changed-source", "changed-pointer"])
def test_bound_prewarm_pins_source_and_never_replaces_a_newer_pointer(monkeypatch, outcome):
    from datetime import datetime

    class BoundData(FakeQueryData):
        def execute(self, sql, params=None):
            if sql == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ":
                self.pinned = True
                return FakeResult()
            return super().execute(sql, params)

    class BoundCache(FakeCache):
        def activate_query_generation_if_current(self, client, artifact, candidate, previous):
            if outcome == "changed-pointer":
                self.generation = "newer"
            if self.generation != previous:
                return False
            self.activate_query_generation(client, artifact, candidate)
            return True

    stamp = "2026-11-07 11:00:00+00"
    data = BoundData(cache_scope="shared", freshness_timestamp=stamp, cache_policy={
        "refresh_mode": "prewarm_swap", "ttl_seconds": 0, "bind_freshness": True,
    })
    data.pinned = False
    _patch_connections(monkeypatch, data=data)
    checks = iter([stamp, "newer" if outcome == "changed-source" else stamp])
    monkeypatch.setattr(engine, "_artifact_cache_freshness_timestamp", lambda *_: next(checks))
    fake_cache = BoundCache(payload=None)
    fake_cache.generation = "previous"
    monkeypatch.setattr(cache_module, "get_cache_settings", lambda: cache_module.CacheSettings(enabled=True))
    monkeypatch.setattr(cache_module, "get_artifact_cache", lambda settings: fake_cache)

    result = engine.prewarm_artifact_query_cache(
        "srp", "rcm-report", queries=[{"operation": "snapshot"}],
        expected_freshness=datetime.fromisoformat(
            "2026-11-06T11:00:00+00:00" if outcome == "wrong-request" else stamp
        ),
    )

    assert data.pinned
    assert result["status"] == ("prewarmed" if outcome == "current" else "superseded")
    if outcome == "current":
        assert len(fake_cache.set_calls) == 1
        key, _, ttl = fake_cache.set_calls[0]
        assert stamp in key and ttl is None
        assert fake_cache.activated_generations == [fake_cache.generation]
    else:
        assert fake_cache.activated_generations == []
        assert fake_cache.generation == ("newer" if outcome == "changed-pointer" else "previous")
        assert len(fake_cache.set_calls) == (0 if outcome == "wrong-request" else 1)
        assert len(fake_cache.invalidated_patterns) == (0 if outcome == "wrong-request" else 1)


@pytest.mark.parametrize("freshness", ["2026-11-07 11:00:00+00", None])
def test_bound_query_miss_does_not_write_a_live_fallback_into_redis(monkeypatch, freshness):
    data = FakeQueryData(cache_scope="shared", freshness_timestamp=freshness, cache_policy={
        "refresh_mode": "prewarm_swap", "ttl_seconds": 0, "bind_freshness": True,
    })
    _patch_connections(monkeypatch, data=data)
    fake_cache = FakeCache(payload=None)
    fake_cache.generation = "existing"
    monkeypatch.setattr(cache_module, "get_cache_settings", lambda: cache_module.CacheSettings(enabled=True))
    monkeypatch.setattr(cache_module, "get_artifact_cache", lambda settings: fake_cache)
    result = engine.execute_artifact_query("srp", "rcm-report", query={"operation": "snapshot"})
    assert result == data.result
    assert fake_cache.set_calls == []


def test_opted_in_set_filters_share_keys_without_changing_query_input():
    browser = {"operation": "facilityComparison", "payers": ["z", "a"], "order": ["z", "a"]}
    worker = {"operation": "facilityComparison", "payers": ["a", "z", "a"], "order": ["z", "a"]}
    policy = {"set_filter_keys": ["payers"]}
    assert engine._canonical_cache_query(browser, policy) == engine._canonical_cache_query(worker, policy)
    assert engine._canonical_cache_query(browser, {}) == browser
    assert browser["payers"] == ["z", "a"]
    assert engine._canonical_cache_query(browser, policy)["order"] == ["z", "a"]


def test_bound_prewarm_serves_equivalent_filter_sets_from_the_persistent_generation(monkeypatch):
    from datetime import datetime

    from fakeredis import FakeRedis

    from bci_redis_cache import RedisCache

    class SnapshotData(FakeQueryData):
        def execute(self, sql, params=None):
            if sql == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ":
                return FakeResult()
            return super().execute(sql, params)

    stamp = "2026-11-11 11:00:00+00"
    data = SnapshotData(cache_scope="shared", freshness_timestamp=stamp, cache_policy={
        "refresh_mode": "prewarm_swap",
        "ttl_seconds": 0,
        "bind_freshness": True,
        "set_filter_keys": ["facilities", "payers"],
    })
    _patch_connections(monkeypatch, data=data)
    cache = RedisCache(redis_client=FakeRedis(decode_responses=True))
    monkeypatch.setattr(cache_module, "get_cache_settings", lambda: cache_module.CacheSettings(enabled=True))
    monkeypatch.setattr(cache_module, "get_artifact_cache", lambda settings: cache)
    requests = [
        {"operation": "detailFacts", "detailScope": "collections",
         "presentationVersion": 2, "facilities": ["b", "a"]},
        {"operation": "detailFacts", "detailScope": "claimFlow",
         "presentationVersion": 3, "payers": ["z", "a"]},
    ]
    result = engine.prewarm_artifact_query_cache(
        "srp", "rcm-report", queries=requests, expected_freshness=datetime.fromisoformat(stamp),
    )
    assert result["entry_count"] == 2
    reordered = [{"facilities": ["a", "b", "a"]}, {"payers": ["a", "z", "a"]}]
    for request, changed in zip(requests, reordered, strict=True):
        assert engine.execute_artifact_query("srp", "rcm-report", query={**request, **changed}) == data.result
    assert data.calls == 2
    assert [json.loads(query) for query in data.query_payloads] == requests
    assert all(cache.redis.ttl(key) == -1 for key in cache.redis.scan_iter())

def test_startup_and_details_share_one_complete_generation(monkeypatch):
    from datetime import datetime

    from fakeredis import FakeRedis

    from bci_redis_cache import RedisCache

    class StartupData(FakeQueryData):
        def execute(self, statement, params=None):
            if statement.startswith("SET TRANSACTION ISOLATION"):
                return FakeResult()
            if "pg_get_viewdef" in statement:
                return FakeResult(row=("fixture-definition",))
            if statement == "SELECT * FROM rpt.visit_counts":
                self.calls += 1
                return FakeResult(
                    rows=[("rcm-report", "live_core", {"surface": "context"})],
                    description=[("artifact_key",), ("data_mode",), ("payload_json",)],
                )
            return super().execute(statement, params)

    class FailingCache(RedisCache):
        fail_view = False

        def set(self, key, value, ttl_seconds=None):
            if self.fail_view and value == {"rows": [{
                "artifact_key": "rcm-report", "data_mode": "live_core", "payload_json": {"surface": "context"},
            }]}:
                raise OSError("synthetic Redis write failure")
            return super().set(key, value, ttl_seconds)

    stamp = "2026-11-11 11:00:00+00"
    data = StartupData(cache_scope="shared", freshness_timestamp=stamp, cache_policy={
        "refresh_mode": "prewarm_swap", "ttl_seconds": 0, "bind_freshness": True,
        "shared_render_view": True, "set_filter_keys": ["facilities", "payers"],
    })
    _patch_connections(monkeypatch, data=data)
    cache = FailingCache(redis_client=FakeRedis(decode_responses=True))
    monkeypatch.setattr(cache_module, "get_cache_settings",
                        lambda: cache_module.CacheSettings(enabled=True, cache_rendered=False))
    monkeypatch.setattr(cache_module, "get_artifact_cache", lambda settings: cache)
    queries = [
        {"operation": "detailFacts", "detailScope": "collections",
         "presentationVersion": 2, "facilities": ["b", "a"]},
        {"operation": "detailFacts", "detailScope": "claimFlow",
         "presentationVersion": 3, "payers": ["z", "a"]},
    ]
    artifact = ("srp", "rcm-report")

    def prewarm(expected=stamp):
        return engine.prewarm_artifact_query_cache(
            *artifact, queries=queries, expected_freshness=datetime.fromisoformat(expected),
        )

    assert prewarm()["render_view_prewarmed"] is True
    active = cache.get_query_generation(*artifact)
    assert data.calls == 3
    preview = engine.execute_artifact(*artifact, behavior="preview",
                                      authenticated_subject="synthetic", authorized_roles=["report-role"])
    assert preview["preview_html"] == "<p>1</p>"
    assert data.authenticated_subjects[-1] == "synthetic"
    for query, reordered in zip(queries, [{"facilities": ["a", "b", "a"]}, {"payers": ["a", "z", "a"]}], strict=True):
        assert engine.execute_artifact_query(*artifact, query={**query, **reordered}) == data.result
    assert data.calls == 3
    assert all(cache.redis.ttl(key) == -1 for key in cache.redis.scan_iter())

    assert prewarm("2026-11-10 11:00:00+00")["status"] == "superseded"
    cache.fail_view = True
    with pytest.raises(OSError, match="Redis write failure"):
        prewarm()
    assert cache.get_query_generation(*artifact) == active
    assert len(list(cache.redis.scan_iter())) == 4
    cache.fail_view = False
    assert prewarm()["render_view_prewarmed"]
    assert cache.get_query_generation(*artifact) != active
