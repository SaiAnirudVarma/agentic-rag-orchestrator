"""
Async integration tests for the API surface.

Run with: pytest -v
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


class TestHealthAndBanner:
    async def test_health_endpoint_reports_ok(self, client):
        resp = await client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["redis"] == "connected"

    async def test_root_banner(self, client):
        resp = await client.get("/")
        assert resp.status_code == 200
        assert "service" in resp.json()


class TestSessionInit:
    async def test_init_session_returns_session_id(self, client):
        resp = await client.post(
            "/agent/session",
            json={"user_id": "user_123", "capabilities": ["context_retrieval"]},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["session_id"].startswith("sess_")
        assert body["user_id"] == "user_123"
        assert body["mcp_protocol_version"]

    async def test_init_session_defaults_capabilities(self, client):
        resp = await client.post("/agent/session", json={"user_id": "user_456"})
        assert resp.status_code == 201
        assert resp.json()["capabilities"] == ["context_retrieval"]

    async def test_init_session_rejects_empty_user_id(self, client):
        resp = await client.post("/agent/session", json={"user_id": ""})
        assert resp.status_code == 422


class TestContextQuery:
    async def test_query_cold_path_hits_mcp(self, client):
        session_resp = await client.post("/agent/session", json={"user_id": "user_789"})
        session_id = session_resp.json()["session_id"]

        resp = await client.post(
            "/agent/query",
            json={"session_id": session_id, "query": "How does the retriever rank chunks?", "top_k": 3},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["cache_hit"] is False
        assert body["routed_via"] == "mcp:vector_context_search"
        assert len(body["results"]) == 3
        assert body["latency_ms"] >= 0

    async def test_query_warm_path_hits_cache(self, client):
        session_resp = await client.post("/agent/session", json={"user_id": "user_789"})
        session_id = session_resp.json()["session_id"]
        query_payload = {"session_id": session_id, "query": "cache me please", "top_k": 2}

        first = await client.post("/agent/query", json=query_payload)
        second = await client.post("/agent/query", json=query_payload)

        assert first.json()["cache_hit"] is False
        assert second.json()["cache_hit"] is True
        assert second.json()["routed_via"] == "redis_cache"
        assert second.json()["results"] == first.json()["results"]

    async def test_query_bypasses_cache_when_disabled(self, client):
        session_resp = await client.post("/agent/session", json={"user_id": "user_999"})
        session_id = session_resp.json()["session_id"]
        payload = {"session_id": session_id, "query": "no cache", "top_k": 1, "use_cache": False}

        first = await client.post("/agent/query", json=payload)
        second = await client.post("/agent/query", json=payload)

        assert first.json()["cache_hit"] is False
        assert second.json()["cache_hit"] is False

    async def test_query_rejects_empty_query(self, client):
        resp = await client.post(
            "/agent/query", json={"session_id": "sess_fake", "query": "", "top_k": 1}
        )
        assert resp.status_code == 422

    async def test_query_rejects_out_of_range_top_k(self, client):
        resp = await client.post(
            "/agent/query", json={"session_id": "sess_fake", "query": "x", "top_k": 999}
        )
        assert resp.status_code == 422


class TestMCPOrchestratorIntegration:
    """Exercises the MCP orchestrator directly, independent of HTTP transport."""

    async def test_handshake_and_tool_listing(self, app):
        orchestrator = app.state.mcp_orchestrator
        session = await orchestrator.handshake(client_capabilities={"requested": ["context_retrieval"]})
        assert session.session_id.startswith("mcp_")

        tools = orchestrator.list_tools()
        tool_names = {t["name"] for t in tools}
        assert "vector_context_search" in tool_names
        assert "sandboxed_code_exec" in tool_names

    async def test_route_request_unknown_tool_raises(self, app):
        from src.mcp_orchestrator import MCPOrchestratorError

        orchestrator = app.state.mcp_orchestrator
        with pytest.raises(MCPOrchestratorError):
            await orchestrator.route_request("does_not_exist", {})
