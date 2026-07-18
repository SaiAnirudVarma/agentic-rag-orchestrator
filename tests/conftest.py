"""
Shared pytest fixtures.

The Redis cache manager is faked rather than mocked with unittest.mock so
the tests exercise real async control flow (await points, cache miss ->
populate -> hit) without requiring a live Redis instance in CI.
"""
from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from src.config import Settings, get_settings
from src.main import create_app
from src.mcp_orchestrator import MCPOrchestrator


class FakeRedisCacheManager:
    """In-memory stand-in for RedisCacheManager, same public interface."""

    def __init__(self, *_args, **_kwargs) -> None:
        self._store: dict[str, dict[str, Any]] = {}
        self.connected = False

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    async def is_healthy(self) -> bool:
        return self.connected

    @staticmethod
    def _key(session_id: str, query: str, top_k: int) -> str:
        return f"{session_id}:{query}:{top_k}"

    async def get_context(self, session_id: str, query: str, top_k: int):
        return self._store.get(self._key(session_id, query, top_k))

    async def set_context(self, session_id: str, query: str, top_k: int, payload, ttl_seconds=None):
        self._store[self._key(session_id, query, top_k)] = payload


@pytest.fixture
def test_settings() -> Settings:
    get_settings.cache_clear()
    return Settings(redis_url="redis://localhost:6379/15", environment="test")


@pytest_asyncio.fixture
async def app(test_settings, monkeypatch):
    """Build a FastAPI app with a faked Redis layer wired into app.state."""
    monkeypatch.setattr("src.main.get_settings", lambda: test_settings)

    fastapi_app = create_app()

    # Override the lifespan-provisioned resources directly for test isolation.
    fake_cache = FakeRedisCacheManager()
    await fake_cache.connect()
    fastapi_app.state.settings = test_settings
    fastapi_app.state.redis_cache = fake_cache
    fastapi_app.state.mcp_orchestrator = MCPOrchestrator(test_settings)

    yield fastapi_app


@pytest_asyncio.fixture
async def client(app):
    """Async HTTP client bound directly to the ASGI app (no network I/O)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac
