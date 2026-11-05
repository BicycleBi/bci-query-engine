import importlib
import sys
from types import SimpleNamespace

import pytest

sys.modules.setdefault("redis", SimpleNamespace(Redis=object))
from bci_redis_cache.cache import RedisCache

cache_module = importlib.import_module("app.cache")


class InvalidatingCache:
    def __init__(self):
        self.patterns = []

    def invalidate_pattern(self, pattern):
        self.patterns.append(pattern)
        return 3


def test_cache_settings_are_disabled_by_default(monkeypatch):
    monkeypatch.delenv("REDIS_ENABLED", raising=False)

    settings = cache_module.get_cache_settings()

    assert settings.enabled is False
    assert settings.host == "redis"
    assert settings.ttl_seconds == 3600
    assert settings.cache_rendered is True


def test_cache_settings_read_redis_env(monkeypatch):
    monkeypatch.setenv("REDIS_ENABLED", "true")
    monkeypatch.setenv("REDIS_HOST", "redis-cache")
    monkeypatch.setenv("REDIS_PORT", "6380")
    monkeypatch.setenv("REDIS_DB", "2")
    monkeypatch.setenv("CACHE_TTL_SECONDS", "120")
    monkeypatch.setenv("CACHE_RENDERED", "false")

    settings = cache_module.get_cache_settings()

    assert settings.enabled is True
    assert settings.host == "redis-cache"
    assert settings.port == 6380
    assert settings.db == 2
    assert settings.ttl_seconds == 120
    assert settings.cache_rendered is False


def test_invalidate_artifact_cache_uses_bci_key_pattern(monkeypatch):
    fake_cache = InvalidatingCache()
    monkeypatch.setattr(cache_module, "get_artifact_cache", lambda: fake_cache)

    removed = cache_module.invalidate_artifact_cache("srp", "visit-counts")

    assert removed == 3
    assert fake_cache.patterns == ["bci:cache:srp:visit-counts:*"]


def test_redis_cache_zero_ttl_writes_without_expiry():
    class Redis:
        def __init__(self):
            self.calls = []

        def set(self, *args, **kwargs):
            self.calls.append((args, kwargs))

    redis = Redis()
    cache = RedisCache(redis_client=redis)

    cache.set("ppp", {"rows": []}, ttl_seconds=0)
    cache.set("other", {"rows": []}, ttl_seconds=3600)

    assert redis.calls[0][1] == {}
    assert redis.calls[1][1] == {"ex": 3600}
    with pytest.raises(ValueError, match="negative"):
        cache.set("invalid", {}, ttl_seconds=-1)


def test_redis_cache_replaces_only_matching_keys_in_one_transaction():
    class Pipeline:
        def __init__(self):
            self.calls = []

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def delete(self, *keys):
            self.calls.append(("delete", keys))

        def set(self, key, value, **kwargs):
            self.calls.append(("set", key, value, kwargs))

        def execute(self):
            self.calls.append(("execute",))

    class Redis:
        def __init__(self):
            self.transaction = Pipeline()
            self.patterns = []

        def scan_iter(self, *, match):
            self.patterns.append(match)
            return iter(["ppp:old"])

        def pipeline(self, *, transaction):
            assert transaction is True
            return self.transaction

    redis = Redis()
    RedisCache(redis_client=redis).replace_pattern(
        "ppp:*", [("ppp:new", {"rows": []})], ttl_seconds=0,
    )

    assert redis.patterns == ["ppp:*"]
    assert redis.transaction.calls == [
        ("delete", ("ppp:old",)),
        ("set", "ppp:new", '{"rows": []}', {}),
        ("execute",),
    ]
