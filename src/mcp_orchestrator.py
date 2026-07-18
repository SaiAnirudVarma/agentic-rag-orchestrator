"""
Model Context Protocol (MCP) orchestration layer.

This module simulates the handshake and routing responsibilities of an
MCP-compatible server: capability negotiation, tool discovery, and
dispatching a context-retrieval request to the appropriate backend
"tool" (vector store, sandboxed code execution, structured DB, etc).

It is deliberately dependency-free (no network I/O) so it can run in
unit tests without a live MCP peer, while mirroring the real MCP
message shapes (initialize / initialized / tools/list / tools/call)
closely enough to swap in a real transport (stdio/SSE/websocket) later.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable

from src.config import Settings

logger = logging.getLogger(__name__)


class MCPErrorCode(int, Enum):
    """Subset of JSON-RPC-style error codes used by MCP transports."""

    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    HANDSHAKE_TIMEOUT = -32001
    TOOL_NOT_FOUND = -32002


class MCPOrchestratorError(Exception):
    """Raised when the MCP handshake or routing layer cannot fulfil a request."""

    def __init__(self, message: str, code: MCPErrorCode) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class MCPTool:
    """Describes a single callable tool exposed by this MCP server."""

    name: str
    description: str
    handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass
class MCPSession:
    """State for a negotiated MCP handshake with a client (an agent)."""

    session_id: str
    protocol_version: str
    client_capabilities: dict[str, Any]
    negotiated_at: float = field(default_factory=time.monotonic)


class MCPOrchestrator:
    """
    Simulates an MCP server: performs the initialize handshake, advertises
    tools, and routes `tools/call` requests to registered handlers.

    In production this class would sit behind a real MCP transport (stdio
    for local subprocess servers, or SSE/websocket for remote servers) and
    the `handshake`/`route_request` methods would be invoked from the
    transport's message loop instead of directly by FastAPI route handlers.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._sessions: dict[str, MCPSession] = {}
        self._tools: dict[str, MCPTool] = {}
        self._register_default_tools()

    # ------------------------------------------------------------------ #
    # Tool registry
    # ------------------------------------------------------------------ #
    def _register_default_tools(self) -> None:
        self.register_tool(
            MCPTool(
                name="vector_context_search",
                description="Retrieve top-k semantically relevant context chunks for a query.",
                handler=self._tool_vector_context_search,
            )
        )
        self.register_tool(
            MCPTool(
                name="sandboxed_code_exec",
                description="Execute agent-generated code inside an isolated Docker/K8s sandbox.",
                handler=self._tool_sandboxed_code_exec,
            )
        )

    def register_tool(self, tool: MCPTool) -> None:
        self._tools[tool.name] = tool
        logger.debug("Registered MCP tool: %s", tool.name)

    def list_tools(self) -> list[dict[str, str]]:
        return [{"name": t.name, "description": t.description} for t in self._tools.values()]

    # ------------------------------------------------------------------ #
    # Handshake (mirrors MCP `initialize` / `initialized`)
    # ------------------------------------------------------------------ #
    async def handshake(self, client_capabilities: dict[str, Any]) -> MCPSession:
        """
        Perform the MCP capability-negotiation handshake with a connecting
        agent client. Real MCP transports exchange an `initialize` request
        and an `initialized` notification; here we simulate that round trip
        with a bounded timeout to mimic network conditions.
        """
        try:
            await asyncio.wait_for(
                self._simulate_handshake_roundtrip(),
                timeout=self._settings.mcp_handshake_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise MCPOrchestratorError(
                "MCP handshake timed out negotiating capabilities.",
                MCPErrorCode.HANDSHAKE_TIMEOUT,
            ) from exc

        session = MCPSession(
            session_id=f"mcp_{uuid.uuid4().hex[:16]}",
            protocol_version=self._settings.mcp_protocol_version,
            client_capabilities=client_capabilities,
        )
        self._sessions[session.session_id] = session
        logger.info(
            "MCP handshake complete session_id=%s protocol=%s",
            session.session_id,
            session.protocol_version,
        )
        return session

    async def _simulate_handshake_roundtrip(self) -> None:
        """Placeholder for the async I/O a real transport would perform."""
        await asyncio.sleep(0)  # yields control; replace with real transport I/O

    def get_session(self, mcp_session_id: str) -> MCPSession | None:
        return self._sessions.get(mcp_session_id)

    # ------------------------------------------------------------------ #
    # Routing (mirrors MCP `tools/call`)
    # ------------------------------------------------------------------ #
    async def route_request(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Dispatch a tool invocation to its registered handler."""
        tool = self._tools.get(tool_name)
        if tool is None:
            raise MCPOrchestratorError(
                f"No MCP tool registered under name '{tool_name}'.",
                MCPErrorCode.TOOL_NOT_FOUND,
            )

        started = time.perf_counter()
        result = await tool.handler(arguments)
        elapsed_ms = (time.perf_counter() - started) * 1000
        logger.debug("MCP tool '%s' completed in %.2fms", tool_name, elapsed_ms)
        return result

    # ------------------------------------------------------------------ #
    # Built-in tool handlers (mock backends)
    # ------------------------------------------------------------------ #
    async def _tool_vector_context_search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """
        Mock vector-store retrieval. A real implementation would call out
        to a vector DB (pgvector, Qdrant, Pinecone, etc.) over the network.
        """
        query = arguments.get("query", "")
        top_k = int(arguments.get("top_k", 5))
        await asyncio.sleep(0.01)  # simulate network I/O to the vector backend

        return {
            "results": [
                {
                    "chunk_id": f"chunk_{i}",
                    "source": "mock_vector_store",
                    "text": f"Simulated context chunk #{i} relevant to: {query!r}",
                    "score": round(max(0.0, 1.0 - i * 0.08), 4),
                }
                for i in range(top_k)
            ]
        }

    async def _tool_sandboxed_code_exec(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """
        Mock sandboxed execution entrypoint. A real implementation would
        submit a Kubernetes Job (or short-lived gVisor/Firecracker
        container) into the `sandbox_namespace` and stream back results.
        """
        code_preview = str(arguments.get("code", ""))[:80]
        await asyncio.sleep(0.01)
        return {
            "status": "executed",
            "namespace": self._settings.sandbox_namespace,
            "stdout": f"[mock sandbox] executed snippet: {code_preview!r}",
            "exit_code": 0,
        }
