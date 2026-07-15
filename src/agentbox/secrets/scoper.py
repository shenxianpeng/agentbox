"""Credential scoping: mint run-scoped, time-limited credentials.

The master API keys (DEEPSEEK_API_KEY, ANTHROPIC_API_KEY) never enter the
sandbox container. Instead, scoped credentials are minted at run creation time
as per-run random tokens. The real API key is stored ONLY in the credential
proxy's in-memory key store, never in the database or the sandbox.

The runner sends the per-run token to the credential-proxy, which replaces
it with the real API key before forwarding the request to the LLM API.

Flow:
  1. API generates per-run token (UUID), stores it in scoped_credentials table
  2. Launcher claims run, registers {per_run_token: real_api_key} with
     credential-proxy via its admin API
  3. Runner uses per-run token as the "API key" and sends requests to
     credential-proxy, which injects the real key
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg

from agentbox.db.queries import insert_scoped_credential

DEFAULT_TTL_SECONDS = 600  # 10 minutes — enough for max_attempts * 120s


def mint_scoped_credentials(
    agent_name: str,
    api_key: str | None = None,  # kept for API compatibility; unused in MVP
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> tuple[str, str, datetime]:
    """Mint a scoped credential for a given agent.

    Returns (per_run_token, scope, expires_at).

    The per_run_token is a random UUID that the runner uses instead of the
    real API key. The fake 'api_key' parameter is accepted for API compatibility
    but the returned token is NEVER the master key.

    In production, this would generate a signed JWT or call a token vault.
    """
    per_run_token = str(uuid.uuid4())
    expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
    scope = f"llm:{agent_name}" if agent_name else "llm:default"
    return per_run_token, scope, expires_at


async def store_scoped_credentials(
    pool: asyncpg.Pool,
    run_id: str,
    api_key: str,  # kept for compat but IGNORED — the real key never enters DB
    agent_name: str,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> list[dict[str, Any]]:
    """Mint and store scoped credentials for a run.

    Returns list of stored credential metadata dicts.
    The stored credential is a per-run token, NOT the master API key.
    The real API key is registered with the credential proxy by the launcher.
    """
    credential, scope, expires_at = mint_scoped_credentials(agent_name, None, ttl_seconds)

    stored = await insert_scoped_credential(pool, run_id, credential, scope, expires_at)
    return [stored]


def build_credentials_json(credentials: list[dict[str, Any]]) -> str:
    """Build the AGENTBOX_CREDENTIALS_JSON string to inject into the runner.

    The runner reads this to configure its LLM clients — it never sees the
    master API keys.
    """
    creds_map = {}
    for cred in credentials:
        creds_map[cred["scope"]] = {
            "credential": cred.get("credential", ""),
            "expires_at": (
                cred["expires_at"].isoformat()
                if hasattr(cred["expires_at"], "isoformat")
                else str(cred["expires_at"])
            ),
        }
    return json.dumps(creds_map)


def build_credentials_response(
    credentials: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build a safe (no credential values) representation for API responses."""
    return [
        {
            "id": str(cred["id"]),
            "scope": cred["scope"],
            "expires_at": (
                cred["expires_at"].isoformat()
                if hasattr(cred["expires_at"], "isoformat")
                else str(cred["expires_at"])
            ),
            "created_at": (
                cred["created_at"].isoformat()
                if hasattr(cred["created_at"], "isoformat")
                else str(cred["created_at"])
            ),
        }
        for cred in credentials
    ]
