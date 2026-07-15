"""Tests for credential scoping logic.

Verifies that:
  - mint_scoped_credentials generates a UUID per-run token (not the master key)
  - build_credentials_json formats correctly
  - build_credentials_response strips credential values
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from agentbox.secrets.scoper import (
    build_credentials_json,
    build_credentials_response,
    mint_scoped_credentials,
)


def test_mint_returns_uuid_not_api_key():
    """The scoped credential must be a UUID, NOT the master API key."""
    token, scope, expires_at = mint_scoped_credentials("incident-investigator", "sk-real-key", 600)
    # Token is a UUID, not the real key
    assert token != "sk-real-key", "Should NOT return the real API key"
    assert "-" in token, "Per-run token should be a UUID"
    assert len(token) == 36, "Per-run token should be UUID length (36 chars)"
    assert scope == "llm:incident-investigator"
    assert expires_at > datetime.now(UTC)


def test_mint_default_scope():
    """When agent_name is empty, scope should default to llm:default."""
    token, scope, expires_at = mint_scoped_credentials("", None, 300)
    assert scope == "llm:default"
    assert expires_at > datetime.now(UTC)


def test_mint_ttl():
    """TTL should be respected."""
    ttl = 120
    token, scope, expires_at = mint_scoped_credentials("test", "key", ttl)
    expected = datetime.now(UTC) + timedelta(seconds=ttl)
    # Allow 2s tolerance
    assert abs((expires_at - expected).total_seconds()) < 2


def test_build_credentials_json():
    """build_credentials_json should format correctly."""
    creds = [
        {
            "scope": "llm:deepseek-chat",
            "credential": "per-run-token",
            "expires_at": datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
        }
    ]
    result = build_credentials_json(creds)
    import json

    parsed = json.loads(result)
    assert "llm:deepseek-chat" in parsed
    assert parsed["llm:deepseek-chat"]["credential"] == "per-run-token"
    assert "expires_at" in parsed["llm:deepseek-chat"]


def test_build_credentials_response_strips_values():
    """API response should NOT include credential values."""
    creds = [
        {
            "id": "abc-123",
            "scope": "llm:deepseek-chat",
            "credential": "super-secret-key",
            "expires_at": datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
            "created_at": datetime(2025, 6, 1, 0, 0, 0, tzinfo=UTC),
        }
    ]
    result = build_credentials_response(creds)
    assert len(result) == 1
    assert "credential" not in result[0], "Response should NOT contain credential values"
    assert result[0]["id"] == "abc-123"
    assert result[0]["scope"] == "llm:deepseek-chat"
