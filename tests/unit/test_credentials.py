"""Tests for runner-side credential loading.

Verifies that:
  - load_credentials reads AGENTBOX_CREDENTIALS_JSON correctly
  - get_llm_api_key extracts the right key by scope
  - Fallback behavior when env var is missing
"""

from __future__ import annotations

import json
import os

from agentbox.runner.credentials import get_llm_api_key, load_credentials


def test_load_credentials_from_env():
    """Should parse AGENTBOX_CREDENTIALS_JSON correctly."""
    creds_data = {
        "llm:deepseek-chat": {
            "credential": "per-run-token-abc",
            "expires_at": "2026-01-01T00:00:00",
        }
    }
    os.environ["AGENTBOX_CREDENTIALS_JSON"] = json.dumps(creds_data)
    try:
        creds = load_credentials()
        assert len(creds) == 1
        assert creds["llm:deepseek-chat"]["credential"] == "per-run-token-abc"
    finally:
        del os.environ["AGENTBOX_CREDENTIALS_JSON"]


def test_load_credentials_empty_env():
    """When env var is missing, should return empty dict."""
    os.environ.pop("AGENTBOX_CREDENTIALS_JSON", None)
    creds = load_credentials()
    assert creds == {}


def test_get_llm_api_key_exact_match():
    """Should return the key for an exact model scope match."""
    creds = {"llm:deepseek-chat": {"credential": "token-123", "expires_at": ""}}
    key = get_llm_api_key(creds, "deepseek-chat")
    assert key == "token-123"


def test_get_llm_api_key_fallback_to_default():
    """Should fall back to llm:default if no model-specific scope."""
    creds = {"llm:default": {"credential": "default-token", "expires_at": ""}}
    key = get_llm_api_key(creds, "unknown-model")
    assert key == "default-token"


def test_get_llm_api_key_not_found():
    """Should return empty string if no matching scope."""
    creds = {"llm:other": {"credential": "token", "expires_at": ""}}
    key = get_llm_api_key(creds, "deepseek-chat")
    assert key == ""
