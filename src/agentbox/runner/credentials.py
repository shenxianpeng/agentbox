"""Scoped credential loader for the sandbox runner.

Reads the AGENTBOX_CREDENTIALS_JSON env var injected by the launcher and
makes scoped credentials available to the agent. The master API key never
enters the sandbox container.

Usage:
    creds = load_credentials()
    deepseek_key = creds.get("llm:incident-investigator", {}).get("credential")
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

CREDENTIALS_ENV_VAR = "AGENTBOX_CREDENTIALS_JSON"


def load_credentials() -> dict[str, dict[str, Any]]:
    """Load scoped credentials from the environment.

    Returns a dict mapping scope names to credential info, e.g.:
        {"llm:incident-investigator": {"credential": "...", "expires_at": "..."}}

    Returns an empty dict if the env var is not set or invalid.
    """
    raw = os.environ.get(CREDENTIALS_ENV_VAR)
    if not raw:
        logger.warning(
            "%s not set — no scoped credentials available. "
            "The agent will not be able to call LLM APIs.",
            CREDENTIALS_ENV_VAR,
        )
        return {}

    try:
        creds: dict[str, dict[str, Any]] = json.loads(raw)
        logger.info("Loaded %d scoped credential(s)", len(creds))
        return creds
    except json.JSONDecodeError as exc:
        logger.error(
            "Failed to parse %s: %s. Falling back to empty credentials.",
            CREDENTIALS_ENV_VAR,
            exc,
        )
        return {}


def get_llm_api_key(creds: dict[str, dict[str, Any]], model_name: str) -> str:
    """Extract the LLM API key from scoped credentials for a given model.

    Falls back to checking the scope key in order:
      1. llm:<model_name>
      2. llm:default
      3. empty string
    """
    # Try model-specific scope first
    scope_key = f"llm:{model_name}"
    if scope_key in creds:
        return creds[scope_key].get("credential", "")

    # Try default LLM scope
    if "llm:default" in creds:
        return creds["llm:default"].get("credential", "")

    return ""
