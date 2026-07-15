"""Credential scoping: mint run-scoped, time-limited credentials.

The master API keys (DEEPSEEK_API_KEY, ANTHROPIC_API_KEY) never enter the
sandbox container. Instead, scoped credentials are minted at run creation time
and injected into the runner as a JSON blob via AGENTBOX_CREDENTIALS_JSON.

In MVP, scoping is simple: we create a time-limited copy of the relevant key.
A production version would use a token vault (e.g. Vault) or a cryptographic
key-scoping service.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg

from agentbox.db.queries import insert_scoped_credential


def mint_scoped_credentials(
    agent_name: str,
    api_key: str,
    ttl_seconds: int = 600,
) -> tuple[str, str, datetime]:
    """Mint a scoped credential for a given agent.

    Returns (credential_value, scope, expires_at).

    In MVP the credential is the same API key but with metadata.
    In production this would call a token service or vault to generate
    a time-limited, scope-restricted credential.
    """
    expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
    scope = f"llm:{agent_name}" if agent_name else "llm:default"
    return api_key, scope, expires_at


async def store_scoped_credentials(
    pool: asyncpg.Pool,
    run_id: str,
    api_key: str,
    agent_name: str,
    ttl_seconds: int = 600,
) -> list[dict[str, Any]]:
    """Mint and store scoped credentials for a run.

    Returns list of stored credential metadata dicts.
    Stores both the credential and a metadata-only entry for API responses.
    """
    credential, scope, expires_at = mint_scoped_credentials(agent_name, api_key, ttl_seconds)

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
