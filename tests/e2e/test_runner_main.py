"""Runner entrypoint tests against a real Postgres.

``agentbox.runner.main`` is exercised end to end through the restricted
``agentbox_runner`` role (Row-Level Security from migration 002), with the
LLM replaced by pydantic-ai's ``TestModel``. No container, proxy or model
API is involved.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from unittest.mock import MagicMock

import asyncpg
import logfire
import pytest
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.models.test import TestModel

from agentbox import tracing
from agentbox.db.queries import get_run, insert_run
from agentbox.runner import main as runner_main
from agentbox.runner.main import (
    ScopedPool,
    _build_model,
    _calculate_total_cost,
    _release_lease,
    _set_run_status,
    _setup_logging,
    get_lease_owner,
    get_run_row,
    heartbeat_lease,
    update_run_result,
)
from agentbox.settings import settings


@pytest.fixture(autouse=True)
def runner_role(monkeypatch):
    """Point the runner at the RLS-restricted role on the test database."""
    runner_url = settings.database_url.replace(
        "://agentbox:agentbox@", "://agentbox_runner:agentbox_runner_dev@"
    )
    assert runner_url != settings.database_url, "test DB must use the agentbox superuser"
    monkeypatch.setattr(settings, "runner_database_url", runner_url)
    monkeypatch.setattr(settings, "logfire_token", "")
    monkeypatch.delenv("HOSTNAME", raising=False)
    monkeypatch.delenv("RUN_ID", raising=False)
    monkeypatch.delenv("AGENTBOX_CREDENTIALS_JSON", raising=False)


async def _lease(pool, run_id: str) -> dict | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM leases WHERE run_id = $1::uuid", run_id)
    return dict(row) if row else None


async def _checkpoints(pool, run_id: str) -> list[dict]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM checkpoints WHERE run_id = $1::uuid ORDER BY step_index", run_id
        )
    return [dict(r) for r in rows]


def _test_model(output: str = "All clear.") -> tuple[TestModel, str]:
    return TestModel(call_tools=[], custom_output_text=output), "test"


# ── full entrypoint ────────────────────────────────────────


async def test_main_without_run_id_exits_1(caplog):
    with caplog.at_level(logging.ERROR):
        assert await runner_main.main() == 1
    assert "RUN_ID not set" in caplog.text


async def test_main_runs_agent_durably_under_rls(pool, monkeypatch):
    run = await insert_run(
        pool, agent_name="incident-investigator", prompt="Check the web service."
    )
    run_id = str(run["id"])
    monkeypatch.setenv("RUN_ID", run_id)
    monkeypatch.setenv(
        "AGENTBOX_CREDENTIALS_JSON",
        json.dumps({"llm:test": {"credential": "per-run-token", "expires_at": ""}}),
    )
    build_model = MagicMock(side_effect=lambda creds: _test_model())
    monkeypatch.setattr(runner_main, "_build_model", build_model)

    assert await runner_main.main() == 0

    build_model.assert_called_once_with(
        {"llm:test": {"credential": "per-run-token", "expires_at": ""}}
    )
    final = await get_run(pool, run_id)
    assert final["status"] == "succeeded"
    assert json.loads(final["result"]) == {"output": "All clear."}
    assert final["error"] is None
    assert final["started_at"] is not None and final["finished_at"] is not None
    checkpoints = await _checkpoints(pool, run_id)
    assert [cp["kind"] for cp in checkpoints] == ["model_call"]
    assert checkpoints[0]["token_count"] > 0
    assert float(final["cost_estimate"]) == pytest.approx(float(checkpoints[0]["cost"]))
    assert await _lease(pool, run_id) is None  # released so the reaper never requeues


async def test_main_uses_default_agent_for_unknown_agent_name(pool, monkeypatch):
    run = await insert_run(pool, agent_name="not-registered", prompt="p")
    monkeypatch.setenv("RUN_ID", str(run["id"]))
    monkeypatch.setattr(runner_main, "_build_model", lambda creds: _test_model("fallback agent"))

    assert await runner_main.main() == 0
    assert json.loads((await get_run(pool, str(run["id"])))["result"]) == {
        "output": "fallback agent"
    }


async def test_main_marks_run_failed_without_credentials(pool, monkeypatch, caplog):
    run = await insert_run(pool, agent_name="incident-investigator", prompt="p")
    run_id = str(run["id"])
    monkeypatch.setenv("RUN_ID", run_id)
    monkeypatch.setattr(settings, "model_name", "deepseek-chat")

    with caplog.at_level(logging.ERROR):
        assert await runner_main.main() == 1

    assert f"Runner failed for run {run_id}" in caplog.text
    final = await get_run(pool, run_id)
    assert final["status"] == "failed"
    assert final["error"].startswith("RuntimeError: No API key available for model deepseek-chat")
    assert "Traceback" in final["error"]
    assert final["result"] is None
    assert await _lease(pool, run_id) is None


async def test_main_releases_lease_when_run_vanishes(pool, monkeypatch, caplog):
    run = await insert_run(pool, agent_name="incident-investigator", prompt="p")
    run_id = str(run["id"])
    monkeypatch.setenv("RUN_ID", run_id)

    async def _gone(pool, run_id):
        return None

    monkeypatch.setattr(runner_main, "get_run_row", _gone)

    with caplog.at_level(logging.ERROR):
        assert await runner_main.main() == 1

    assert f"Run {run_id} not found in database" in caplog.text
    assert await _lease(pool, run_id) is None
    assert (await get_run(pool, run_id))["status"] == "queued"  # untouched


# ── helpers ────────────────────────────────────────────────


async def test_heartbeat_lease_refreshes_and_survives_errors(pool, monkeypatch, caplog):
    run = await insert_run(pool, agent_name="hb", prompt="p")
    run_id = str(run["id"])
    await get_lease_owner(pool, run_id)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE leases SET heartbeat_at = now() - interval '1 hour' WHERE run_id = $1::uuid",
            run_id,
        )
    monkeypatch.setattr(runner_main, "LEASE_HEARTBEAT_INTERVAL", 0.01)

    task = asyncio.create_task(heartbeat_lease(pool, run_id))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    async with pool.acquire() as conn:
        age = await conn.fetchval(
            "SELECT extract(epoch FROM now() - heartbeat_at) FROM leases WHERE run_id = $1::uuid",
            run_id,
        )
    assert age < 5

    broken = MagicMock()
    broken.acquire.side_effect = RuntimeError("connection lost")
    task = asyncio.create_task(heartbeat_lease(broken, run_id))
    with caplog.at_level(logging.ERROR):
        await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert f"Failed to heartbeat lease for run {run_id}" in caplog.text
    assert broken.acquire.call_count > 1  # kept retrying after the error


async def test_get_lease_owner_uses_hostname_and_upserts(pool, monkeypatch):
    run = await insert_run(pool, agent_name="owner", prompt="p")
    run_id = str(run["id"])

    generated = await get_lease_owner(pool, run_id)
    uuid.UUID(generated)  # falls back to a fresh UUID without HOSTNAME
    assert (await _lease(pool, run_id))["owner"] == generated

    monkeypatch.setenv("HOSTNAME", "runner-pod-7")
    assert await get_lease_owner(pool, run_id) == "runner-pod-7"
    assert (await _lease(pool, run_id))["owner"] == "runner-pod-7"

    await _release_lease(pool, run_id)
    assert await _lease(pool, run_id) is None


async def test_get_run_row_and_status_updates(pool):
    assert await get_run_row(pool, str(uuid.uuid4())) is None

    run = await insert_run(pool, agent_name="rows", prompt="p")
    run_id = str(run["id"])
    row = await get_run_row(pool, run_id)
    assert row["agent_name"] == "rows" and row["status"] == "queued"

    await _set_run_status(pool, run_id, "running")
    row = await get_run_row(pool, run_id)
    assert row["status"] == "running"

    await update_run_result(pool, run_id, "failed", error="boom")
    final = await get_run(pool, run_id)
    assert final["status"] == "failed" and final["error"] == "boom"
    assert final["result"] is None and final["cost_estimate"] is None

    await update_run_result(pool, run_id, "succeeded", result="done", cost_estimate=0.25)
    final = await get_run(pool, run_id)
    assert json.loads(final["result"]) == {"output": "done"}
    assert float(final["cost_estimate"]) == pytest.approx(0.25)


async def test_calculate_total_cost_sums_checkpoints(pool):
    run = await insert_run(pool, agent_name="cost", prompt="p")
    run_id = str(run["id"])
    assert await _calculate_total_cost(pool, run_id) == 0.0

    async with pool.acquire() as conn:
        for idx, cost in ((0, 0.001), (1, None), (2, 0.002)):
            await conn.execute(
                """
                INSERT INTO checkpoints (run_id, step_index, kind, fingerprint, payload, cost)
                VALUES ($1::uuid, $2, 'tool_call', 'fp', '{}'::jsonb, $3)
                """,
                run_id,
                idx,
                cost,
            )
    assert await _calculate_total_cost(pool, run_id) == pytest.approx(0.003)


async def test_scoped_pool_sets_run_id_on_every_acquire(pool):
    scoped = ScopedPool(pool, "run-xyz")
    async with scoped.acquire() as conn:
        assert await conn.fetchval("SELECT current_setting('app.run_id', true)") == "run-xyz"
    async with scoped.acquire() as conn:
        assert await conn.fetchval("SELECT current_setting('app.run_id', true)") == "run-xyz"

    inner = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=1)
    await ScopedPool(inner, "r").close()
    with pytest.raises(asyncpg.InterfaceError, match="pool is closed"):
        async with inner.acquire():
            pass


def test_setup_logging_configures_logfire_only_with_token(monkeypatch):
    mocks = {
        name: MagicMock(name=name)
        for name in (
            "configure",
            "instrument_asyncpg",
            "instrument_httpx",
            "instrument_pydantic_ai",
            "info",
        )
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(logfire, name, mock)
    attach = MagicMock(name="attach_traceparent_from_env")
    monkeypatch.setattr(tracing, "attach_traceparent_from_env", attach)

    _setup_logging("run-1")
    mocks["configure"].assert_not_called()
    attach.assert_not_called()

    monkeypatch.setattr(settings, "logfire_token", "lf-token")
    _setup_logging("run-1")
    mocks["configure"].assert_called_once_with(token="lf-token", service_name="agentbox-runner")
    mocks["instrument_asyncpg"].assert_called_once_with()
    mocks["instrument_httpx"].assert_called_once_with()
    mocks["instrument_pydantic_ai"].assert_called_once_with()
    attach.assert_called_once_with()
    mocks["info"].assert_called_once_with("Runner starting", run_id="run-1")


def test_build_model_routes_through_credential_proxy(monkeypatch):
    monkeypatch.setattr(settings, "model_name", "deepseek-chat")
    creds = {"llm:deepseek-chat": {"credential": "per-run-token", "expires_at": ""}}

    monkeypatch.setenv("CREDENTIAL_PROXY_URL", "http://proxy.test:9090")
    model, name = _build_model(creds)
    assert name == "deepseek-chat"
    assert isinstance(model, OpenAIChatModel)
    assert model.model_name == "deepseek-chat"
    assert model.base_url == "http://proxy.test:9090"
    assert model.client.api_key == "per-run-token"

    monkeypatch.delenv("CREDENTIAL_PROXY_URL")
    model, _ = _build_model({"llm:default": {"credential": "fallback-token"}})
    assert model.base_url == "http://credential-proxy:9090"

    with pytest.raises(RuntimeError, match="No API key available for model deepseek-chat"):
        _build_model({})
