"""
Agentic Systems & Custom RAG Orchestrator — FastAPI application entrypoint.

This service exposes an async API for:
    * initializing agent sessions (performs an MCP handshake), and
    * running high-throughput contextual retrieval queries backed by a
      Redis cache in front of an MCP-routed vector search tool.

Application-scoped resources (the Redis connection pool and the MCP
orchestrator) are created once during the FastAPI `lifespan` and attached
to `app.state`, then shared across requests via dependency accessors in
the routers — no global singletons, no per-request connection churn.
"""
from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from src.config import get_settings
from src.logging_config import configure_logging
from src.mcp_orchestrator import MCPOrchestrator
from src.redis_cache import RedisCacheManager
from src.routers.agent_router import router as agent_router

configure_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manage startup/shutdown of shared, process-wide resources."""
    settings = get_settings()
    logger.info("Starting %s v%s (%s)", settings.app_name, settings.app_version, settings.environment)

    redis_cache = RedisCacheManager(settings)
    await redis_cache.connect()

    mcp_orchestrator = MCPOrchestrator(settings)

    app.state.settings = settings
    app.state.redis_cache = redis_cache
    app.state.mcp_orchestrator = mcp_orchestrator

    yield

    logger.info("Shutting down %s", settings.app_name)
    await redis_cache.disconnect()


def create_app() -> FastAPI:
    """Application factory — keeps import-time side effects out of module scope."""
    settings = get_settings()

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description=(
            "Async orchestration layer for agentic RAG: MCP-based tool "
            "routing, Redis-backed context caching, and sandboxed "
            "execution hooks for Docker/Kubernetes."
        ),
        lifespan=lifespan,
    )

    app.include_router(agent_router)

    @app.middleware("http")
    async def add_process_time_header(request: Request, call_next):
        """Attach request latency to every response for lightweight observability."""
        started = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Process-Time-Ms"] = f"{(time.perf_counter() - started) * 1000:.2f}"
        return response

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        """Never leak stack traces; log server-side, return a stable error contract."""
        logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Internal server error."},
        )

    @app.get("/health", tags=["ops"], summary="Liveness/readiness probe")
    async def health(request: Request) -> dict[str, object]:
        """
        Kubernetes-friendly health endpoint. Reports Redis connectivity so
        readiness probes can catch a degraded cache dependency without
        killing the pod (Redis is a soft dependency — see redis_cache.py).
        """
        redis_cache: RedisCacheManager = request.app.state.redis_cache
        redis_healthy = await redis_cache.is_healthy()
        return {
            "status": "ok",
            "service": settings.app_name,
            "version": settings.app_version,
            "redis": "connected" if redis_healthy else "degraded",
        }

    @app.get("/", tags=["ops"], summary="Service banner")
    async def root() -> dict[str, str]:
        return {"service": settings.app_name, "version": settings.app_version, "docs": "/docs"}

    return app


app = create_app()
