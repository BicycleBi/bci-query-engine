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

    def set(
        self,
        key: str,
        value: dict[str, Any],
        ttl_seconds: Optional[int] = 3600,
    ) -> None:
        encoded = json.dumps(value, sort_keys=True, default=str)
        if ttl_seconds is None or ttl_seconds == 0:
            self.redis.set(key, encoded)
            return
        self.redis.set(key, encoded, ex=ttl_seconds)

    def publish_load_bound_payload(self, key: str, value: dict[str, Any]) -> bool:
        """Atomic single-entry replacement; an older completion cannot win.

        The active payload has no TTL. Failed writes retain the previous value;
        replacement uses one bounded key rather than accumulating generations.
        """
        script = """
        local old = redis.call('GET', KEYS[1])
        if old then
          local ok, current = pcall(cjson.decode, old)
          if ok and type(current) == 'table' and tonumber(current.generation_epoch) and
             tonumber(current.generation_epoch) > tonumber(ARGV[2]) then return 0 end
        end
        redis.call('SET', KEYS[1], ARGV[1])
        return 1
        """
        return bool(self.redis.eval(script, 1, key,
            json.dumps(value, sort_keys=True, default=str), value['generation_epoch']))

    @staticmethod
    def _query_generation_key(client_key: str, artifact_key: str) -> str:
        return f"bci:cache:{client_key}:{artifact_key}:artifact-query-generation"

    def get_query_generation(self, client_key: str, artifact_key: str) -> Optional[str]:
        return self.redis.get(self._query_generation_key(client_key, artifact_key))

    def activate_query_generation(
        self,
        client_key: str,
        artifact_key: str,
        generation: str,
    ) -> Optional[str]:
        return self.redis.getset(
            self._query_generation_key(client_key, artifact_key),
            generation,
        )

    def invalidate_pattern(self, pattern: str) -> int:
        removed = 0
        for key in self.redis.scan_iter(match=pattern):
            removed += int(self.redis.delete(key))
        return removed
