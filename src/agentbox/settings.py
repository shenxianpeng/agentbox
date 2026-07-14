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

    # ── Cost tracking (Phase 2) ───────────────────────────────
    cost_per_1k_input_tokens: float = 0.15  # USD, default for deepseek
    cost_per_1k_output_tokens: float = 0.60  # USD
    compute_cost_per_second: float = 0.0000167  # ~$0.06/hr


settings = Settings()  # type: ignore[call-arg]
