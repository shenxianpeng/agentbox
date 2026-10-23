"""Launcher worker tests against a real Postgres: leases, reaping, claims.

The sandbox backend is a recorder (no container is started) and the
credential proxy registration is stubbed; everything else — claim query,
lease upserts, requeue/fail transitions, dead-lease detection — runs
against the real schema.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any
from unittest.mock import AsyncMock

import logfire
import pytest

from agentbox.db.queries import get_run, insert_run
from agentbox.launcher import worker
from agentbox.launcher.worker import (
    _fetch_scoped_credentials,
    _handle_claimed_run,
    claim_next_run,
    create_lease,
    fail_run,
    get_dead_leases,
    get_running_count,
    get_tenant_ids,
    release_lease,
    requeue_run,
)
from agentbox.secrets.scoper import store_scoped_credentials
from agentbox.settings import settings
from agentbox.tracing import capture_traceparent

DEFAULT_TENANT = "00000000-0000-0000-0000-000000000001"


@pytest.fixture(scope="module", autouse=True)
def _configure_logfire():
    """Local (non-sending) Logfire so launcher spans and traceparents are real."""
    logfire.configure(send_to_logfire=False, console=False)


class _StopLoopError(Exception):
    pass


class _RecordingBackend:
    """Sandbox backend double: records start/kill calls, optionally fails."""

    def __init__(self, start_error: Exception | None = None, kill_error: str | None = None):
        self.started: list[dict[str, Any]] = []
        self.killed: list[str] = []
        self._start_error = start_error
        self._kill_error = kill_error

    def start_run(self, **kwargs: Any) -> str:
        self.started.append(kwargs)
        if self._start_error:
            raise self._start_error
        return "container-1"

    def kill_run(self, run_id: str) -> bool:
        self.killed.append(run_id)
        if self._kill_error == run_id:
            raise RuntimeError("docker daemon unavailable")
        return True


async def _create_tenant(pool, max_concurrent: int = 5) -> str:
    tenant_id = str(uuid.uuid4())
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO tenants (id, name, max_concurrent) VALUES ($1::uuid, $2, $3)",
            tenant_id,
            f"tenant-{tenant_id[:8]}",
            max_concurrent,
        )
    return tenant_id


async def _lease_row(pool, run_id: str) -> dict | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM leases WHERE run_id = $1::uuid", run_id)
    return dict(row) if row else None


async def _age_lease(pool, run_id: str, seconds: int) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE leases SET heartbeat_at = now() - make_interval(secs => $2) "
            "WHERE run_id = $1::uuid",
            run_id,
            seconds,
        )


async def _set_run(pool, run_id: str, **fields: Any) -> None:
    assignments = ", ".join(f"{k} = ${i + 2}" for i, k in enumerate(fields))
    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE runs SET {assignments} WHERE id = $1::uuid", run_id, *fields.values()
        )


# ── plain queries ──────────────────────────────────────────


async def test_get_tenant_ids_includes_default_tenant(pool):
    tenant_id = await _create_tenant(pool)
    tenants = await get_tenant_ids(pool)
    assert tenants[0] == DEFAULT_TENANT  # oldest first
    assert tenant_id in tenants


async def test_lease_lifecycle(pool):
    run = await insert_run(pool, agent_name="lease-test", prompt="p")
    run_id = str(run["id"])

    owner = await create_lease(pool, run_id)
    assert owner.startswith("launcher-")
    assert (await _lease_row(pool, run_id))["owner"] == owner

    # Re-creating the lease takes it over (ON CONFLICT ... DO UPDATE)
    owner2 = await create_lease(pool, run_id)
    assert owner2 != owner
    assert (await _lease_row(pool, run_id))["owner"] == owner2

    await release_lease(pool, run_id)
    assert await _lease_row(pool, run_id) is None


async def test_fail_and_requeue_transitions(pool):
    run = await insert_run(pool, agent_name="transition-test", prompt="p")
    run_id = str(run["id"])
    await _set_run(pool, run_id, status="running")

    await fail_run(pool, run_id, "boom")
    failed = await get_run(pool, run_id)
    assert failed["status"] == "failed"
    assert failed["error"] == "boom"
    assert failed["finished_at"] is not None

    await _set_run(pool, run_id, status="running")
    await requeue_run(pool, run_id)
    queued = await get_run(pool, run_id)
    assert queued["status"] == "queued"
    assert queued["started_at"] is None


async def test_get_dead_leases_only_returns_stale_running_runs(pool):
    stale = await insert_run(pool, agent_name="stale", prompt="p")
    fresh = await insert_run(pool, agent_name="fresh", prompt="p")
    finished = await insert_run(pool, agent_name="finished", prompt="p")
    for run in (stale, fresh, finished):
        await _set_run(pool, str(run["id"]), status="running", attempt=2)
        await create_lease(pool, str(run["id"]))
    await _age_lease(pool, str(stale["id"]), worker.LEASE_TTL_SECONDS + 30)
    await _age_lease(pool, str(finished["id"]), worker.LEASE_TTL_SECONDS + 30)
    await _set_run(pool, str(finished["id"]), status="succeeded")

    dead = {str(lease["run_id"]): lease for lease in await get_dead_leases(pool)}

    assert str(stale["id"]) in dead
    assert dead[str(stale["id"])]["attempt"] == 2
    assert dead[str(stale["id"])]["max_attempts"] == 3
    assert str(fresh["id"]) not in dead
    assert str(finished["id"]) not in dead


async def test_claim_next_run_defaults_to_default_tenant(pool):
    run = await insert_run(pool, agent_name="default-claim", prompt="p", egress_allow=["a.example"])
    assert list(run["egress_allow"]) == ["a.example"]

    # Other tests leave runs of the default tenant in 'running'; lift its
    # concurrency limit for the duration of this claim so it is deterministic.
    async with pool.acquire() as conn:
        previous = await conn.fetchval(
            "UPDATE tenants SET max_concurrent = 10000 WHERE id = $1::uuid "
            "RETURNING (SELECT max_concurrent FROM tenants WHERE id = $1::uuid)",
            DEFAULT_TENANT,
        )
    try:
        claimed = await claim_next_run(pool)
    finally:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE tenants SET max_concurrent = $2 WHERE id = $1::uuid",
                DEFAULT_TENANT,
                previous,
            )

    assert claimed is not None
    assert str(claimed["tenant_id"]) == DEFAULT_TENANT
    assert claimed["attempt"] >= 1
    assert (await get_run(pool, str(claimed["id"])))["status"] == "running"


async def test_get_running_count_counts_running_runs(pool):
    before = await get_running_count(pool)
    run = await insert_run(pool, agent_name="count", prompt="p")
    await _set_run(pool, str(run["id"]), status="running")
    assert await get_running_count(pool) == before + 1


async def test_fetch_scoped_credentials_formats_runner_json(pool):
    run = await insert_run(pool, agent_name="creds", prompt="p")
    run_id = str(run["id"])
    [stored] = await store_scoped_credentials(pool, run_id, "sk-master", "deepseek-chat")

    creds = await _fetch_scoped_credentials(pool, run_id)

    assert list(creds) == ["llm:deepseek-chat"]
    assert creds["llm:deepseek-chat"]["expires_at"] == stored["expires_at"].isoformat()
    token = creds["llm:deepseek-chat"]["credential"]
    assert token != "sk-master" and len(token) == 36  # per-run UUID, never the master key
    assert await _fetch_scoped_credentials(pool, str(uuid.uuid4())) == {}


# ── _handle_claimed_run ────────────────────────────────────


@pytest.fixture
def proxy_stubs(monkeypatch):
    register, unregister = AsyncMock(), AsyncMock()
    monkeypatch.setattr(worker, "_register_with_credential_proxy", register)
    monkeypatch.setattr(worker, "_unregister_from_credential_proxy", unregister)
    monkeypatch.setattr(settings, "deepseek_api_key", "sk-deepseek-master")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "credential_proxy_url", "http://credential-proxy:9090")
    return register, unregister


async def _claim_fresh_run(pool, *, max_attempts: int = 3, with_creds: bool = True):
    tenant_id = await _create_tenant(pool)
    with logfire.span("api-request"):
        traceparent = capture_traceparent()
    run = await insert_run(
        pool,
        agent_name="incident-investigator",
        prompt="p",
        tenant_id=tenant_id,
        traceparent=traceparent,
    )
    run_id = str(run["id"])
    await _set_run(pool, run_id, max_attempts=max_attempts)
    if with_creds:
        await store_scoped_credentials(pool, run_id, "", "deepseek-chat")
    claimed = await claim_next_run(pool, tenant_id)
    assert claimed is not None and str(claimed["id"]) == run_id
    return claimed, run_id, traceparent


async def test_handle_claimed_run_starts_sandbox_with_scoped_token(pool, proxy_stubs):
    register, unregister = proxy_stubs
    run, run_id, api_traceparent = await _claim_fresh_run(pool)
    backend = _RecordingBackend()

    await _handle_claimed_run(pool, backend, run)

    assert (await _lease_row(pool, run_id))["owner"].startswith("launcher-")
    [start] = backend.started
    assert start["run_id"] == run_id
    assert start["database_url"] == settings.database_url
    assert start["credential_proxy_url"] == "http://credential-proxy:9090"
    creds = json.loads(start["scoped_credentials"])
    token = creds["llm:deepseek-chat"]["credential"]
    assert "sk-deepseek-master" not in start["scoped_credentials"]
    # The launcher continues the API request's trace into the runner
    assert start["traceparent"].split("-")[1] == api_traceparent.split("-")[1]
    register.assert_awaited_once_with(
        token,
        "sk-deepseek-master",
        "https://api.deepseek.com",
        creds["llm:deepseek-chat"]["expires_at"],
    )
    unregister.assert_not_awaited()
    assert (await get_run(pool, run_id))["status"] == "running"


async def test_handle_claimed_run_without_api_key_or_credentials(
    pool, proxy_stubs, monkeypatch, caplog
):
    register, _ = proxy_stubs
    monkeypatch.setattr(settings, "deepseek_api_key", "")
    run, run_id, _ = await _claim_fresh_run(pool, with_creds=False)
    backend = _RecordingBackend()

    with caplog.at_level(logging.WARNING):
        await _handle_claimed_run(pool, backend, run)

    assert "No LLM API key configured" in caplog.text
    register.assert_not_awaited()
    assert backend.started[0]["scoped_credentials"] == "{}"

    # With a key but no stored credential there is nothing to register either
    monkeypatch.setattr(settings, "deepseek_api_key", "sk")
    run2, _, _ = await _claim_fresh_run(pool, with_creds=False)
    await _handle_claimed_run(pool, _RecordingBackend(), run2)
    register.assert_not_awaited()


async def test_handle_claimed_run_requeues_when_sandbox_start_fails(pool, proxy_stubs, caplog):
    _, unregister = proxy_stubs
    run, run_id, _ = await _claim_fresh_run(pool)
    backend = _RecordingBackend(start_error=RuntimeError("image not found"))

    with caplog.at_level(logging.ERROR):
        await _handle_claimed_run(pool, backend, run)

    assert f"Failed to start container for run {run_id}: image not found" in caplog.text
    unregister.assert_awaited_once()
    assert await _lease_row(pool, run_id) is None
    requeued = await get_run(pool, run_id)
    assert requeued["status"] == "queued"
    assert requeued["attempt"] == 1  # kept so the next claim counts as attempt 2


async def test_handle_claimed_run_fails_on_last_attempt(pool, proxy_stubs):
    run, run_id, _ = await _claim_fresh_run(pool, max_attempts=1, with_creds=False)
    backend = _RecordingBackend(start_error=RuntimeError("image not found"))

    await _handle_claimed_run(pool, backend, run)

    failed = await get_run(pool, run_id)
    assert failed["status"] == "failed"
    assert failed["error"] == "Container start failed: image not found"
    assert await _lease_row(pool, run_id) is None


# ── reaper_loop against the real schema ────────────────────


async def test_reaper_loop_requeues_and_fails_dead_leases(pool, monkeypatch):
    retry = await insert_run(pool, agent_name="retry", prompt="p")
    dead = await insert_run(pool, agent_name="dead", prompt="p")
    alive = await insert_run(pool, agent_name="alive", prompt="p")
    await _set_run(pool, str(retry["id"]), status="running", attempt=1)
    await _set_run(pool, str(dead["id"]), status="running", attempt=3)
    await _set_run(pool, str(alive["id"]), status="running", attempt=1)
    for run in (retry, dead, alive):
        await create_lease(pool, str(run["id"]))
    await _age_lease(pool, str(retry["id"]), worker.LEASE_TTL_SECONDS + 30)
    await _age_lease(pool, str(dead["id"]), worker.LEASE_TTL_SECONDS + 30)

    async def _sleep(delay: float) -> None:
        assert delay == worker.REAPER_INTERVAL
        raise _StopLoopError()

    monkeypatch.setattr(worker.asyncio, "sleep", _sleep)
    backend = _RecordingBackend(kill_error=str(retry["id"]))

    with pytest.raises(_StopLoopError):
        await worker.reaper_loop(pool, backend)

    # Other tests may leave stale leases behind; only our three runs matter here
    assert {str(retry["id"]), str(dead["id"])} <= set(backend.killed)
    assert str(alive["id"]) not in backend.killed
    assert (await get_run(pool, str(retry["id"])))["status"] == "queued"
    assert await _lease_row(pool, str(retry["id"])) is None
    failed = await get_run(pool, str(dead["id"]))
    assert failed["status"] == "failed"
    assert failed["error"].startswith("Run exceeded max attempts (3).")
    assert await _lease_row(pool, str(dead["id"])) is None
    assert (await get_run(pool, str(alive["id"])))["status"] == "running"
    assert await _lease_row(pool, str(alive["id"])) is not None
