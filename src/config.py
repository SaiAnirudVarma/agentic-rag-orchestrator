"""
Centralized application configuration.

Uses pydantic-settings so all runtime configuration is validated at process
startup and can be overridden via environment variables / .env file, which
is the standard 12-factor pattern for production services.
"""
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Strongly typed application settings, sourced from the environment."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Service metadata -------------------------------------------------
    app_name: str = "Agentic RAG Orchestrator"
    app_version: str = "0.1.0"
    environment: str = Field(default="development", description="development|staging|production")
    log_level: str = Field(default="INFO")

    # --- Redis --------------------------------------------------------------
    redis_url: str = Field(default="redis://localhost:6379/0")
    redis_cache_ttl_seconds: int = Field(default=300, description="TTL for cached context payloads")
    redis_max_connections: int = Field(default=20)

    # --- MCP orchestration ----------------------------------------------------
    mcp_server_name: str = Field(default="agentic-rag-mcp-server")
    mcp_protocol_version: str = Field(default="2024-11-05")
    mcp_handshake_timeout_seconds: float = Field(default=5.0)

    # --- Sandboxed execution (Docker/K8s) --------------------------------
    sandbox_execution_enabled: bool = Field(default=True)
    sandbox_namespace: str = Field(default="agentic-rag-sandbox")


@lru_cache
def get_settings() -> Settings:
    """Return a cached, process-wide Settings instance."""
    return Settings()
