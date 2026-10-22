"""Unit tests for the launcher worker loops and credential-proxy helpers.

The database functions and the sandbox backend are replaced by recorders;
the infinite loops are stopped by making ``asyncio.sleep`` raise after a
fixed number of polls. The credential proxy is an in-process
``httpx.MockTransport``. No container, cluster or network is touched.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import logfire
import pytest

from agentbox.launcher import backend_docker, backend_k8s, worker
from agentbox.settings import settings

DEFAULT_TENANT = "00000000-0000-0000-0000-000000000001"


class _StopLoopError(Exception):
    """Raised from the patched ``asyncio.sleep`` to leave a ``while True`` loop."""


@pytest.fixture
def stop_after(monkeypatch):
    """Patch ``asyncio.sleep`` to record delays and raise on the n-th call."""
    delays: list[float] = []

    def _install(n: int) -> list[float]:
        async def _sleep(delay: float, *_args: Any, **_kwargs: Any) -> None:
            delays.append(delay)
            if len(delays) >= n:
                raise _StopLoopError()

        monkeypatch.setattr(asyncio, "sleep", _sleep)
        return delays

    return _install


@pytest.fixture
def proxy_http(monkeypatch):
    """Serve the credential proxy admin API from an httpx MockTransport."""
    state: dict[str, Any] = {"response": httpx.Response(200, json={}), "requests": []}
    real_client = httpx.AsyncClient

    def _handler(request: httpx.Request) -> httpx.Response:
        state["requests"].append(request)
        if isinstance(state["response"], Exception):
            raise state["response"]
        return state["response"]

    def _client(**kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(_handler))
        return real_client(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _client)
    monkeypatch.setattr(settings, "credential_proxy_url", "http://proxy.test:9090")
    monkeypatch.setattr(settings, "agentbox_api_token", "admin-token")
    return state


# ── launcher_loop ──────────────────────────────────────────


async def test_launcher_loop_round_robins_tenants_and_claims(monkeypatch, stop_after):
    delays = stop_after(2)
    monkeypatch.setattr(worker, "get_running_count", AsyncMock(return_value=0))
    monkeypatch.setattr(worker, "get_tenant_ids", AsyncMock(return_value=["t1", "t2"]))
    claim_order: list[str] = []

    async def _claim(pool, tenant_id):
        claim_order.append(tenant_id)
        return {"id": f"run-{tenant_id}"} if tenant_id == "t2" else None

    monkeypatch.setattr(worker, "claim_next_run", _claim)
    handled = AsyncMock()
    monkeypatch.setattr(worker, "_handle_claimed_run", handled)
    pool, backend = object(), object()

    with pytest.raises(_StopLoopError):
        await worker.launcher_loop(pool, backend)

    # Poll 1 starts at t1 (miss) then t2 (hit, break); poll 2 resumes the
    # round-robin at t1 (miss) then t2 (hit): fairness across tenants.
    assert claim_order == ["t1", "t2", "t1", "t2"]
    assert handled.await_count == 2
    assert handled.await_args.args == (pool, backend, {"id": "run-t2"})
    assert delays == [worker.POLL_INTERVAL] * 2


async def test_launcher_loop_defaults_to_default_tenant(monkeypatch, stop_after, caplog):
    stop_after(1)
    monkeypatch.setattr(worker, "get_running_count", AsyncMock(return_value=0))
    monkeypatch.setattr(worker, "get_tenant_ids", AsyncMock(return_value=[]))
    claim = AsyncMock(return_value=None)
    monkeypatch.setattr(worker, "claim_next_run", claim)
    handled = AsyncMock()
    monkeypatch.setattr(worker, "_handle_claimed_run", handled)

    with caplog.at_level(logging.DEBUG, logger="agentbox.launcher.worker"):
        with pytest.raises(_StopLoopError):
            await worker.launcher_loop(object(), object())

    claim.assert_awaited_once()
    assert claim.await_args.args[1] == DEFAULT_TENANT
    handled.assert_not_awaited()
    assert "No queued runs found" in caplog.text


async def test_launcher_loop_waits_at_max_concurrent(monkeypatch, stop_after, caplog):
    stop_after(1)
    monkeypatch.setattr(settings, "max_concurrent_runs", 2)
    monkeypatch.setattr(worker, "get_running_count", AsyncMock(return_value=2))
    claim = AsyncMock()
    monkeypatch.setattr(worker, "claim_next_run", claim)

    with caplog.at_level(logging.DEBUG, logger="agentbox.launcher.worker"):
        with pytest.raises(_StopLoopError):
            await worker.launcher_loop(object(), object())

    claim.assert_not_awaited()
    assert "At max concurrent (2), waiting..." in caplog.text


async def test_launcher_loop_survives_poll_errors(monkeypatch, stop_after, caplog):
    stop_after(2)
    monkeypatch.setattr(
        worker, "get_running_count", AsyncMock(side_effect=[RuntimeError("db down"), 0])
    )
    monkeypatch.setattr(worker, "get_tenant_ids", AsyncMock(return_value=["t1"]))
    monkeypatch.setattr(worker, "claim_next_run", AsyncMock(return_value={"id": "r"}))
    handled = AsyncMock()
    monkeypatch.setattr(worker, "_handle_claimed_run", handled)

    with caplog.at_level(logging.ERROR):
        with pytest.raises(_StopLoopError):
            await worker.launcher_loop(object(), object())

    assert "Error in launcher poll loop: db down" in caplog.text
    handled.assert_awaited_once()  # the loop recovered on the next poll


# ── reaper_loop ────────────────────────────────────────────


async def test_reaper_loop_requeues_or_fails_dead_leases(monkeypatch, stop_after, caplog):
    stop_after(1)
    dead = [
        {"run_id": "run-retry", "attempt": 1, "max_attempts": 3, "heartbeat_at": "t0"},
        {"run_id": "run-dead", "attempt": 3, "max_attempts": 3, "heartbeat_at": "t1"},
    ]
    monkeypatch.setattr(worker, "get_dead_leases", AsyncMock(return_value=dead))
    release, requeue, fail = AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(worker, "release_lease", release)
    monkeypatch.setattr(worker, "requeue_run", requeue)
    monkeypatch.setattr(worker, "fail_run", fail)
    backend = MagicMock()
    backend.kill_run.side_effect = [RuntimeError("no docker"), True]
    pool = object()

    with caplog.at_level(logging.WARNING):
        with pytest.raises(_StopLoopError):
            await worker.reaper_loop(pool, backend)

    assert [c.args for c in backend.kill_run.call_args_list] == [("run-retry",), ("run-dead",)]
    assert "Failed to kill container for run run-retry: no docker" in caplog.text
    assert [c.args for c in release.await_args_list] == [(pool, "run-retry"), (pool, "run-dead")]
    requeue.assert_awaited_once_with(pool, "run-retry")
    fail.assert_awaited_once()
    assert fail.await_args.args[:2] == (pool, "run-dead")
    assert "Run exceeded max attempts (3)" in fail.await_args.args[2]
    assert "Last lease heartbeat: t1" in fail.await_args.args[2]


async def test_reaper_loop_survives_errors(monkeypatch, stop_after, caplog):
    delays = stop_after(1)
    monkeypatch.setattr(worker, "get_dead_leases", AsyncMock(side_effect=RuntimeError("db down")))

    with caplog.at_level(logging.ERROR):
        with pytest.raises(_StopLoopError):
            await worker.reaper_loop(object(), MagicMock())

    assert "Error in reaper loop: db down" in caplog.text
    assert delays == [worker.REAPER_INTERVAL]


# ── main ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("backend_name", "module", "cls_name", "token"),
    [
        ("docker", backend_docker, "DockerBackend", ""),
        ("k8s", backend_k8s, "K8sBackend", "lf-token"),
    ],
)
async def test_main_wires_backend_and_loops(monkeypatch, backend_name, module, cls_name, token):
    monkeypatch.setattr(settings, "agentbox_backend", backend_name)
    monkeypatch.setattr(settings, "logfire_token", token)
    monkeypatch.setattr(settings, "database_url", "postgresql://db.test/agentbox")
    pool, backend = object(), object()
    create_pool = AsyncMock(return_value=pool)
    monkeypatch.setattr(worker, "create_pool", create_pool)
    monkeypatch.setattr(module, cls_name, lambda: backend)
    other = backend_k8s if module is backend_docker else backend_docker
    monkeypatch.setattr(
        other,
        "K8sBackend" if other is backend_k8s else "DockerBackend",
        MagicMock(side_effect=AssertionError("wrong backend")),
    )
    configure = MagicMock()
    monkeypatch.setattr(logfire, "configure", configure)
    monkeypatch.setattr(logfire, "instrument_asyncpg", MagicMock())
    monkeypatch.setattr(logfire, "instrument_httpx", MagicMock())
    started: list[tuple[str, Any, Any]] = []

    async def _launcher(p, b):
        started.append(("launcher", p, b))

    async def _reaper(p, b):
        started.append(("reaper", p, b))

    monkeypatch.setattr(worker, "launcher_loop", _launcher)
    monkeypatch.setattr(worker, "reaper_loop", _reaper)

    await worker.main()

    create_pool.assert_awaited_once_with("postgresql://db.test/agentbox")
    assert sorted(started) == [("launcher", pool, backend), ("reaper", pool, backend)]
    if token:
        configure.assert_called_once_with(token="lf-token", service_name="agentbox-launcher")
        logfire.instrument_asyncpg.assert_called_once_with()
        logfire.instrument_httpx.assert_called_once_with()
    else:
        configure.assert_not_called()


# ── credential proxy helpers ───────────────────────────────


def test_get_llm_api_key_and_base_url(monkeypatch):
    monkeypatch.setattr(settings, "deepseek_api_key", "sk-deepseek")
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant")
    assert worker._get_llm_api_key_and_base_url() == ("sk-deepseek", "https://api.deepseek.com")

    monkeypatch.setattr(settings, "deepseek_api_key", "")
    assert worker._get_llm_api_key_and_base_url() == ("sk-ant", "https://api.anthropic.com")

    monkeypatch.setattr(settings, "anthropic_api_key", "")
    assert worker._get_llm_api_key_and_base_url() is None


async def test_register_with_credential_proxy_success(proxy_http, caplog):
    with caplog.at_level(logging.INFO):
        await worker._register_with_credential_proxy(
            "per-run-token-1234", "sk-real", "https://api.deepseek.com", "2026-01-01T00:00:00"
        )

    request = proxy_http["requests"][0]
    assert request.method == "POST"
    assert str(request.url) == "http://proxy.test:9090/admin/keys"
    assert request.headers["Authorization"] == "Bearer admin-token"
    assert request.read() == (
        b'{"run_token":"per-run-token-1234","api_key":"sk-real",'
        b'"base_url":"https://api.deepseek.com","expires_at":"2026-01-01T00:00:00"}'
    )
    assert "Registered key with credential proxy (token prefix: per-run-...)" in caplog.text


async def test_register_with_credential_proxy_without_expiry_and_errors(proxy_http, caplog):
    proxy_http["response"] = httpx.Response(500, text="store full")
    with caplog.at_level(logging.ERROR):
        await worker._register_with_credential_proxy("tok", "sk", "https://api.deepseek.com")
    assert b'"expires_at"' not in proxy_http["requests"][0].read()
    assert "Failed to register key with credential proxy: 500 store full" in caplog.text

    proxy_http["response"] = httpx.ConnectError("refused")
    with caplog.at_level(logging.WARNING):
        await worker._register_with_credential_proxy("tok", "sk", "https://api.deepseek.com")
    assert "Credential proxy unavailable: refused (runs will still work)" in caplog.text


async def test_unregister_from_credential_proxy(proxy_http):
    await worker._unregister_from_credential_proxy("per-run-token-1234")

    request = proxy_http["requests"][0]
    assert request.method == "DELETE"
    assert str(request.url) == "http://proxy.test:9090/admin/keys/per-run-token-1234"
    assert request.headers["Authorization"] == "Bearer admin-token"

    proxy_http["response"] = httpx.ConnectError("refused")
    await worker._unregister_from_credential_proxy("per-run-token-1234")  # swallowed
    assert len(proxy_http["requests"]) == 2
