"""
Async Redis cache manager.

Wraps redis-py's asyncio client behind a small application-specific
interface so the rest of the codebase never touches the Redis client
directly. This keeps connection pooling, key namespacing, serialization,
and failure handling centralized and unit-testable.

Design notes:
    * A single connection pool is created once at application startup
      (see main.py lifespan) and reused across requests — no per-request
      connections.
    * Cache keys are namespaced and hashed so arbitrary/long query text
      never leaks into Redis key names.
    * Cache failures are treated as soft failures: a Redis outage should
      degrade retrieval latency, not take the API down. Callers get a
      cache miss instead of a raised exception.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

import redis.asyncio as redis
from redis.asyncio import Redis
from redis.exceptions import RedisError

from src.config import Settings

logger = logging.getLogger(__name__)

_KEY_PREFIX = "ctx"


class RedisCacheManager:
    """Manages a pooled async Redis connection and context-payload caching."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: Redis | None = None

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def connect(self) -> None:
        """Initialize the connection pool. Idempotent."""
        if self._client is not None:
            return

        self._client = redis.from_url(
            self._settings.redis_url,
            max_connections=self._settings.redis_max_connections,
            decode_responses=True,
            socket_connect_timeout=2.0,
            socket_timeout=2.0,
        )
        try:
            await self._client.ping()
            logger.info("Connected to Redis at %s", self._settings.redis_url)
        except RedisError:
            logger.warning(
                "Redis ping failed on startup; continuing in degraded mode.",
                exc_info=True,
            )

    async def disconnect(self) -> None:
        """Close the connection pool cleanly on application shutdown."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None
            logger.info("Redis connection pool closed.")

    async def is_healthy(self) -> bool:
        """Lightweight liveness check used by the /health endpoint."""
        if self._client is None:
            return False
        try:
            return bool(await self._client.ping())
        except RedisError:
            return False

    # ------------------------------------------------------------------ #
    # Cache operations
    # ------------------------------------------------------------------ #
    @staticmethod
    def _build_key(session_id: str, query: str, top_k: int) -> str:
        """Deterministic, collision-resistant cache key for a retrieval query."""
        digest = hashlib.sha256(f"{query}:{top_k}".encode("utf-8")).hexdigest()
        return f"{_KEY_PREFIX}:{session_id}:{digest}"

    async def get_context(self, session_id: str, query: str, top_k: int) -> dict[str, Any] | None:
        """Fetch a cached retrieval payload. Returns None on miss or failure."""
        if self._client is None:
            return None

        key = self._build_key(session_id, query, top_k)
        try:
            raw = await self._client.get(key)
        except RedisError:
            logger.warning("Redis GET failed for key=%s", key, exc_info=True)
            return None

        if raw is None:
            return None

        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.error("Corrupt cache payload for key=%s; dropping entry.", key)
            await self._safe_delete(key)
            return None

    async def set_context(
        self,
        session_id: str,
        query: str,
        top_k: int,
        payload: dict[str, Any],
        ttl_seconds: int | None = None,
    ) -> None:
        """Cache a retrieval payload with a TTL. Failures are logged, not raised."""
        if self._client is None:
            return

        key = self._build_key(session_id, query, top_k)
        ttl = ttl_seconds or self._settings.redis_cache_ttl_seconds
        try:
            await self._client.set(key, json.dumps(payload), ex=ttl)
        except RedisError:
            logger.warning("Redis SET failed for key=%s", key, exc_info=True)

    async def _safe_delete(self, key: str) -> None:
        if self._client is None:
            return
        try:
            await self._client.delete(key)
        except RedisError:
            pass
