import json
import logging
from hashlib import sha256
from typing import Any

from redis import Redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)


class RedisCache:
    def __init__(
        self,
        redis_url: str | None,
        ttl_seconds: float,
        namespace: str,
        client: Redis | None = None,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.namespace = namespace
        self.enabled = bool(redis_url) and ttl_seconds > 0
        self.client = client
        if self.enabled and self.client is None:
            self.client = Redis.from_url(
                redis_url,
                decode_responses=True,
                socket_connect_timeout=1,
                socket_timeout=1,
            )

    def get(self, key: str) -> Any | None:
        if not self.enabled or self.client is None:
            return None
        try:
            value = self.client.get(self._redis_key(key))
            if value is None:
                return None
            if isinstance(value, bytes):
                value = value.decode()
            return json.loads(value)
        except (RedisError, ValueError, TypeError) as exc:
            logger.warning("Redis cache read failed; continuing without cache: %s", exc)
            return None

    def set(self, key: str, value: Any) -> None:
        if not self.enabled or self.client is None:
            return
        try:
            payload = json.dumps(value, sort_keys=True, separators=(",", ":"))
            ttl_milliseconds = max(1, int(self.ttl_seconds * 1000))
            self.client.set(self._redis_key(key), payload, px=ttl_milliseconds)
        except (RedisError, TypeError, ValueError) as exc:
            logger.warning("Redis cache write failed; continuing without cache: %s", exc)

    def _redis_key(self, key: str) -> str:
        digest = sha256(key.encode()).hexdigest()
        return f"travel-planner:{self.namespace}:{digest}"
