"""Redis-backed JSON cache helpers."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

from redis import Redis as RedisClient


class RedisCache:
    """Small Redis cache wrapper matching Query Engine's cache contract."""

    def __init__(
        self,
        redis_client: Optional[RedisClient] = None,
        *,
        host: str = "localhost",
        port: int = 6379,
        db: int = 0,
        password: Optional[str] = None,
        socket_timeout: float = 2.0,
        socket_connect_timeout: float = 2.0,
        ssl: bool = False,
        ssl_ca_certs: Optional[str] = None,
    ) -> None:
        self.redis = redis_client or RedisClient(
            host=host,
            port=port,
            db=db,
            password=password,
            socket_timeout=socket_timeout,
            socket_connect_timeout=socket_connect_timeout,
            ssl=ssl,
            ssl_ca_certs=ssl_ca_certs,
            decode_responses=True,
        )

    @staticmethod
    def build_key(client_key: str, artifact_key: str, cache_type: str, params: dict[str, Any]) -> str:
        fingerprint = hashlib.sha256(
            json.dumps(params, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        ).hexdigest()
        return f"bci:cache:{client_key}:{artifact_key}:{cache_type}:{fingerprint}"

    def ping(self) -> bool:
        return bool(self.redis.ping())

    def get(self, key: str) -> Optional[dict[str, Any]]:
        raw = self.redis.get(key)
        if raw is None:
            return None
        value = json.loads(raw)
        if not isinstance(value, dict):
            return None
        return value

    def set(self, key: str, value: dict[str, Any], ttl_seconds: int = 3600) -> None:
        if ttl_seconds < 0:
            raise ValueError("Cache TTL must not be negative")
        encoded = json.dumps(value, sort_keys=True, default=str)
        if ttl_seconds == 0:
            self.redis.set(key, encoded)
        else:
            self.redis.set(key, encoded, ex=ttl_seconds)

    def invalidate_pattern(self, pattern: str) -> int:
        removed = 0
        for key in self.redis.scan_iter(match=pattern):
            removed += int(self.redis.delete(key))
        return removed

    def replace_pattern(
        self, pattern: str, entries: list[tuple[str, dict[str, Any]]],
        *, ttl_seconds: int,
    ) -> None:
        """Publish a prepared artifact query generation in one Redis transaction."""
        if ttl_seconds < 0:
            raise ValueError("Cache TTL must not be negative")
        old_keys = list(self.redis.scan_iter(match=pattern))
        with self.redis.pipeline(transaction=True) as transaction:
            if old_keys:
                transaction.delete(*old_keys)
            for key, value in entries:
                encoded = json.dumps(value, sort_keys=True, default=str)
                if ttl_seconds == 0:
                    transaction.set(key, encoded)
                else:
                    transaction.set(key, encoded, ex=ttl_seconds)
            transaction.execute()
