"""
Pydantic request/response schemas shared across routers.

Kept separate from route handlers so the wire contract can be reused by
client SDKs, tests, and OpenAPI generation without importing FastAPI.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


class AgentCapability(str, Enum):
    """Capabilities an agent session may be granted at init time."""

    CONTEXT_RETRIEVAL = "context_retrieval"
    TOOL_EXECUTION = "tool_execution"
    SANDBOXED_CODE_EXEC = "sandboxed_code_exec"
    MEMORY_WRITE = "memory_write"


class SessionInitRequest(BaseModel):
    """Payload to initialize a new agent session."""

    user_id: str = Field(..., min_length=1, description="Caller/tenant identifier")
    capabilities: list[AgentCapability] = Field(
        default_factory=lambda: [AgentCapability.CONTEXT_RETRIEVAL],
        description="Capabilities requested for this session",
    )
    metadata: dict[str, str] = Field(default_factory=dict)


class SessionInitResponse(BaseModel):
    """Result of a successful agent session initialization."""

    session_id: str
    user_id: str
    capabilities: list[AgentCapability]
    mcp_server: str
    mcp_protocol_version: str
    created_at: datetime


class ContextQueryRequest(BaseModel):
    """Payload to run a contextual retrieval query within a session."""

    session_id: str = Field(..., description="Active session returned by /agent/session")
    query: str = Field(..., min_length=1, max_length=4096)
    top_k: int = Field(default=5, ge=1, le=50)
    use_cache: bool = Field(default=True, description="Serve from Redis cache when available")


class RetrievedChunk(BaseModel):
    """A single retrieved context chunk."""

    chunk_id: str
    source: str
    text: str
    score: float


class ContextQueryResponse(BaseModel):
    """Result of a contextual retrieval query."""

    session_id: str
    query: str
    results: list[RetrievedChunk]
    cache_hit: bool
    latency_ms: float
    routed_via: str


def new_session_id() -> str:
    return f"sess_{uuid.uuid4().hex[:20]}"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
