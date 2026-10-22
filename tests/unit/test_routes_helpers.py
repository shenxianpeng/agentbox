"""Unit tests for the control-plane route helpers (no DB needed)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from agentbox.api.routes import _run_to_response, _serialize_dt, verify_token
from agentbox.settings import settings


def test_serialize_dt():
    assert _serialize_dt(None) is None
    assert _serialize_dt(datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)) == "2026-01-02T03:04:05+00:00"
    assert _serialize_dt("2026-01-02") == "2026-01-02"


def test_verify_token_open_access_when_no_token_configured(monkeypatch):
    monkeypatch.setattr(settings, "agentbox_api_token", "")
    assert verify_token(None) is None


def test_verify_token_rejects_missing_or_wrong_token(monkeypatch):
    monkeypatch.setattr(settings, "agentbox_api_token", "secret")

    with pytest.raises(HTTPException) as missing:
        verify_token(None)
    assert missing.value.status_code == 401
    assert missing.value.detail == "Missing authorization header"

    with pytest.raises(HTTPException) as wrong:
        verify_token(HTTPAuthorizationCredentials(scheme="Bearer", credentials="nope"))
    assert wrong.value.status_code == 401
    assert wrong.value.detail == "Invalid token"

    assert verify_token(HTTPAuthorizationCredentials(scheme="Bearer", credentials="secret")) is None


def test_run_to_response_handles_missing_optional_fields():
    run = {
        "id": "11111111-2222-3333-4444-555555555555",
        "status": "queued",
        "tenant_id": "00000000-0000-0000-0000-000000000001",
        "agent_name": "a",
        "prompt": "p",
        "attempt": 0,
        "max_attempts": 3,
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    resp = _run_to_response(run)
    assert resp["egress_allow"] == []
    assert resp["cost_estimate"] is None
    assert resp["started_at"] is None and resp["finished_at"] is None
    assert "credentials" not in resp

    run["cost_estimate"] = "0.000123"
    resp = _run_to_response(run, creds=[])
    assert resp["cost_estimate"] == pytest.approx(0.000123)
    assert resp["credentials"] == []
