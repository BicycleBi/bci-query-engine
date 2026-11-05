import importlib

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
