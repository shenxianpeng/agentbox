"""Application settings loaded from environment variables."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Database ──────────────────────────────────────────────
    database_url: str = "postgresql://agentbox:agentbox@localhost:5432/agentbox"
    # Restricted URL for the runner (uses agentbox_runner role with RLS)
    runner_database_url: str = "postgresql://agentbox_runner:agentbox_runner_dev@localhost:5432/agentbox"

    # ── API ───────────────────────────────────────────────────
    agentbox_api_token: str = "dev-token"

    # ── Runner ────────────────────────────────────────────────
    runner_image: str = "agentbox-runner:latest"
    model_name: str = "deepseek-chat"

    # ── LLM API Keys ──────────────────────────────────────────
    deepseek_api_key: str = ""
    anthropic_api_key: str = ""

    # ── Observability ─────────────────────────────────────────
    logfire_token: str = ""

    # ── Launcher / Scheduling ────────────────────────────────
    agentbox_backend: str = "docker"  # "docker" | "k8s"
    max_concurrent_runs: int = 3
    default_tenant_max_concurrent: int = 5
    warm_pool_size: int = 0  # Phase 2

    # ── Credential proxy ─────────────────────────────────────
    credential_proxy_url: str = "http://credential-proxy:9090"

    # ── Cost tracking (Phase 2) ───────────────────────────────
    cost_per_1k_input_tokens: float = 0.00027  # USD, default for deepseek-chat
    cost_per_1k_output_tokens: float = 0.00110  # USD
    compute_cost_per_second: float = 0.0000167  # ~$0.06/hr


settings = Settings()  # type: ignore[call-arg]
