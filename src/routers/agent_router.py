"""
Agent session & contextual retrieval endpoints.

Routes are thin: they validate input via Pydantic, delegate to the Redis
cache and MCP orchestrator (both injected via FastAPI dependencies bound
to app.state in main.py), and shape the response. No business logic lives
in the transport layer.
"""
from __future__ import annotations

import logging
import time

from fastapi import APIRouter, HTTPException, Request, status

from src.mcp_orchestrator import MCPOrchestrator, MCPOrchestratorError
from src.models.schemas import (
    ContextQueryRequest,
    ContextQueryResponse,
    RetrievedChunk,
    SessionInitRequest,
    SessionInitResponse,
    new_session_id,
    utcnow,
)
from src.redis_cache import RedisCacheManager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agent", tags=["agent"])


def _get_cache(request: Request) -> RedisCacheManager:
    return request.app.state.redis_cache


def _get_orchestrator(request: Request) -> MCPOrchestrator:
    return request.app.state.mcp_orchestrator


@router.post(
    "/session",
    response_model=SessionInitResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Initialize a new agent session",
)
async def init_session(payload: SessionInitRequest, request: Request) -> SessionInitResponse:
    """
    Initialize an agent session by performing the MCP handshake and
    minting a session identifier the client will attach to subsequent
    /agent/query calls.
    """
    orchestrator = _get_orchestrator(request)

    try:
        mcp_session = await orchestrator.handshake(
            client_capabilities={"requested": [c.value for c in payload.capabilities]}
        )
    except MCPOrchestratorError as exc:
        logger.error("MCP handshake failed for user_id=%s: %s", payload.user_id, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"MCP handshake failed: {exc}",
        ) from exc

    session_id = new_session_id()
    logger.info(
        "Agent session initialized session_id=%s user_id=%s mcp_session=%s",
        session_id,
        payload.user_id,
        mcp_session.session_id,
    )

    return SessionInitResponse(
        session_id=session_id,
        user_id=payload.user_id,
        capabilities=payload.capabilities,
        mcp_server=orchestrator._settings.mcp_server_name,  # noqa: SLF001 - internal read-only access
        mcp_protocol_version=mcp_session.protocol_version,
        created_at=utcnow(),
    )


@router.post(
    "/query",
    response_model=ContextQueryResponse,
    summary="Run a contextual retrieval query for an active session",
)
async def run_context_query(payload: ContextQueryRequest, request: Request) -> ContextQueryResponse:
    """
    Execute a contextual search query for an existing agent session.

    Flow:
        1. Check Redis for a cached payload (fast path, sub-millisecond).
        2. On a miss, route the request through the MCP orchestrator to
           the `vector_context_search` tool.
        3. Populate the cache for subsequent identical queries.
    """
    cache = _get_cache(request)
    orchestrator = _get_orchestrator(request)
    started = time.perf_counter()

    cache_hit = False
    cached = None
    if payload.use_cache:
        cached = await cache.get_context(payload.session_id, payload.query, payload.top_k)

    if cached is not None:
        cache_hit = True
        results_raw = cached["results"]
        routed_via = "redis_cache"
    else:
        try:
            tool_result = await orchestrator.route_request(
                "vector_context_search",
                {"query": payload.query, "top_k": payload.top_k},
            )
        except MCPOrchestratorError as exc:
            logger.error("MCP routing failed for session_id=%s: %s", payload.session_id, exc)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"MCP tool routing failed: {exc}",
            ) from exc

        results_raw = tool_result["results"]
        routed_via = "mcp:vector_context_search"

        if payload.use_cache:
            await cache.set_context(
                payload.session_id, payload.query, payload.top_k, {"results": results_raw}
            )

    latency_ms = (time.perf_counter() - started) * 1000
    logger.info(
        "Context query served session_id=%s cache_hit=%s latency_ms=%.2f",
        payload.session_id,
        cache_hit,
        latency_ms,
    )

    return ContextQueryResponse(
        session_id=payload.session_id,
        query=payload.query,
        results=[RetrievedChunk(**r) for r in results_raw],
        cache_hit=cache_hit,
        latency_ms=round(latency_ms, 3),
        routed_via=routed_via,
    )
