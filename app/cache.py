"""
cache.py - Redis cache support for Query Engine artifact rendering.
"""
from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CacheSettings:
    enabled: bool
    host: str = "redis"
    port: int = 6379
    db: int = 0
    password: Optional[str] = None
    socket_timeout: float = 2.0
    socket_connect_timeout: float = 2.0
    ssl: bool = False
    ssl_ca_certs: Optional[str] = None
    ttl_seconds: int = 3600
    cache_rendered: bool = True


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _optional_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    return int(raw)


def _optional_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    return float(raw)


def _redis_password() -> Optional[str]:
    password = os.environ.get("REDIS_PASSWORD")
    if password:
        return password

    password_file = os.environ.get("REDIS_PASSWORD_FILE")
    if password_file:
        return Path(password_file).read_text(encoding="utf-8").strip()

    return None


def get_cache_settings() -> CacheSettings:
    return CacheSettings(
        enabled=_truthy(os.environ.get("REDIS_ENABLED", "false")),
        host=os.environ.get("REDIS_HOST", "redis"),
        port=_optional_int("REDIS_PORT", 6379),
        db=_optional_int("REDIS_DB", 0),
        password=_redis_password(),
        socket_timeout=_optional_float("REDIS_SOCKET_TIMEOUT_SECONDS", 2.0),
        socket_connect_timeout=_optional_float("REDIS_CONNECT_TIMEOUT_SECONDS", 2.0),
        ssl=_truthy(os.environ.get("REDIS_SSL", "false")),
        ssl_ca_certs=os.environ.get("REDIS_SSL_CA_CERTS") or None,
        ttl_seconds=_optional_int("CACHE_TTL_SECONDS", 3600),
        cache_rendered=_truthy(os.environ.get("CACHE_RENDERED", "true")),
    )


def get_artifact_cache(settings: Optional[CacheSettings] = None):
    settings = settings or get_cache_settings()
    if not settings.enabled:
        return None

    try:
        from bci_redis_cache import RedisCache
    except ImportError:
        logger.warning("Redis cache enabled but bci-redis-cache is not installed")
        return None

    try:
        cache = RedisCache(
            host=settings.host,
            port=settings.port,
            db=settings.db,
            password=settings.password,
            socket_timeout=settings.socket_timeout,
            socket_connect_timeout=settings.socket_connect_timeout,
            ssl=settings.ssl,
            ssl_ca_certs=settings.ssl_ca_certs,
        )
        if not cache.ping():
            logger.warning("Redis cache enabled but Redis ping failed")
            return None
        return cache
    except Exception as exc:
        logger.warning("Redis cache unavailable; falling back to normal execution: %s", exc)
        return None


def build_render_cache_params(
    *,
    behavior: str,
    view_name: str,
    template_body: str,
    template_id: Optional[str],
    render_artifact_id: str,
) -> dict[str, Any]:
    template_hash = hashlib.sha256(template_body.encode("utf-8")).hexdigest()
    return {
        "cache_version": 1,
        "behavior": behavior,
        "view_name": view_name,
        "template_id": template_id,
        "template_hash": template_hash,
        "render_artifact_id": render_artifact_id,
    }


def get_cached_render(cache, client_key: str, artifact_key: str, params: dict[str, Any]) -> Optional[dict[str, Any]]:
    try:
        key = cache.build_key(client_key, artifact_key, "rendered", params)
        cached = cache.get(key)
        if cached is not None:
            logger.info("Redis cache hit for artifact render client=%s artifact=%s", client_key, artifact_key)
        else:
            logger.info("Redis cache miss for artifact render client=%s artifact=%s", client_key, artifact_key)
        return cached
    except Exception as exc:
        logger.warning("Redis cache read failed; falling back to normal execution: %s", exc)
        return None


def set_cached_render(
    cache,
    client_key: str,
    artifact_key: str,
    params: dict[str, Any],
    *,
    html: str,
    row_count: int,
    ttl_seconds: int,
) -> None:
    try:
        key = cache.build_key(client_key, artifact_key, "rendered", params)
        cache.set(key, {"html": html, "row_count": row_count}, ttl_seconds=ttl_seconds)
        logger.info("Redis cache set for artifact render client=%s artifact=%s", client_key, artifact_key)
    except Exception as exc:
        logger.warning("Redis cache write failed; continuing without cache write: %s", exc)


def invalidate_artifact_cache(client_key: str, artifact_key: str) -> int:
    cache = get_artifact_cache()
    if cache is None:
        return 0

    pattern = f"bci:cache:{client_key}:{artifact_key}:*"
    try:
        removed = int(cache.invalidate_pattern(pattern))
        logger.info(
            "Redis cache invalidated for artifact client=%s artifact=%s keys=%s",
            client_key,
            artifact_key,
            removed,
        )
        return removed
    except Exception as exc:
        logger.warning("Redis cache invalidation failed; continuing: %s", exc)
        return 0
