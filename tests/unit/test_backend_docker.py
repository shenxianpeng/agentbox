"""Unit tests for the Docker launcher backend.

The Docker SDK is fully mocked: no daemon is contacted and no container is
ever started. The tests pin down the sandbox hardening that ``start_run``
applies and the kill / reap / log paths of the backend.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

import docker
from agentbox.launcher.backend_docker import (
    CONTAINER_LABEL_PREFIX,
    NETWORK_NAME,
    DockerBackend,
)
from agentbox.settings import settings

RUN_ID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def client(monkeypatch) -> MagicMock:
    """Replace ``docker.from_env`` with a mock client."""
    fake = MagicMock(name="docker_client")
    monkeypatch.setattr(docker, "from_env", lambda: fake)
    return fake


@pytest.fixture
def backend(client) -> DockerBackend:
    return DockerBackend()


def _container(status: str = "running", container_id: str | None = "abc123def456") -> MagicMock:
    c = MagicMock(name=f"container-{status}")
    c.status = status
    c.id = container_id
    c.short_id = (container_id or "")[:12]
    return c


# ── start_run ──────────────────────────────────────────────


def test_start_run_builds_hardened_container(backend, client, monkeypatch):
    monkeypatch.setenv("EGRESS_PROXY_HOST", "proxy.internal")
    monkeypatch.setenv("EGRESS_PROXY_PORT", "3128")
    monkeypatch.setattr(settings, "runner_image", "agentbox-runner:test")
    monkeypatch.setattr(settings, "model_name", "deepseek-chat")
    monkeypatch.setattr(settings, "logfire_token", "lf-token")
    monkeypatch.setattr(
        settings,
        "runner_database_url",
        "postgresql://agentbox_runner:pw@localhost:5432/agentbox",
    )
    client.containers.run.return_value = _container()

    container_id = backend.start_run(
        run_id=RUN_ID,
        database_url="postgresql://agentbox:agentbox@localhost:5432/agentbox",
        scoped_credentials='{"llm:default": {"credential": "per-run-token"}}',
        credential_proxy_url="http://credential-proxy:9090",
        traceparent="00-abc-def-01",
    )

    assert container_id == "abc123def456"
    client.containers.run.assert_called_once()
    kwargs = client.containers.run.call_args.kwargs
    assert kwargs["image"] == "agentbox-runner:test"
    assert kwargs["labels"] == {CONTAINER_LABEL_PREFIX: RUN_ID}
    assert kwargs["network"] == NETWORK_NAME
    assert kwargs["detach"] is True
    # Sandbox hardening
    assert kwargs["read_only"] is True
    assert kwargs["cap_add"] == []
    assert kwargs["pids_limit"] == 100
    assert kwargs["mem_limit"] == "512m"
    assert kwargs["cpu_quota"] == kwargs["cpu_period"] == 100000
    assert kwargs["tmpfs"] == {"/tmp": ""}
    assert kwargs["extra_hosts"] == {"host.docker.internal": "host-gateway"}

    env = kwargs["environment"]
    assert env["RUN_ID"] == RUN_ID
    # localhost is rewritten to the compose service name for the runner
    assert env["RUNNER_DATABASE_URL"] == "postgresql://agentbox_runner:pw@postgres:5432/agentbox"
    assert env["AGENTBOX_CREDENTIALS_JSON"] == '{"llm:default": {"credential": "per-run-token"}}'
    assert env["MODEL_NAME"] == "deepseek-chat"
    assert env["LOGFIRE_TOKEN"] == "lf-token"
    assert env["CREDENTIAL_PROXY_URL"] == "http://credential-proxy:9090"
    assert env["TRACEPARENT"] == "00-abc-def-01"
    assert env["HTTP_PROXY"] == env["HTTPS_PROXY"] == "http://proxy.internal:3128"
    assert "postgres" in env["NO_PROXY"] and "credential-proxy" in env["NO_PROXY"]
    # The master database URL passed by the launcher never enters the sandbox env
    assert "postgresql://agentbox:agentbox@" not in " ".join(env.values())


def test_start_run_applies_env_overrides_and_defaults(backend, client, monkeypatch):
    monkeypatch.delenv("EGRESS_PROXY_HOST", raising=False)
    monkeypatch.delenv("EGRESS_PROXY_PORT", raising=False)
    client.containers.run.return_value = _container(container_id=None)

    container_id = backend.start_run(
        run_id=RUN_ID,
        database_url="postgresql://x",
        scoped_credentials="{}",
        env_overrides={"EXTRA": "1", "MODEL_NAME": "override-model"},
    )

    assert container_id == ""  # container.id was None
    env = client.containers.run.call_args.kwargs["environment"]
    assert env["EXTRA"] == "1"
    assert env["MODEL_NAME"] == "override-model"
    assert env["TRACEPARENT"] == ""
    assert env["CREDENTIAL_PROXY_URL"] == ""
    assert env["HTTP_PROXY"] == "http://egress-proxy:8888"


# ── kill_run ───────────────────────────────────────────────


def test_kill_run_returns_false_without_container(backend, client, caplog):
    client.containers.list.return_value = []

    with caplog.at_level(logging.WARNING):
        assert backend.kill_run(RUN_ID) is False

    assert f"No container found for run {RUN_ID}" in caplog.text
    client.containers.list.assert_called_once_with(
        filters={"label": f"{CONTAINER_LABEL_PREFIX}={RUN_ID}"}, all=True
    )


def test_kill_run_kills_running_and_removes_all(backend, client, caplog):
    running = _container("running", "aaa111")
    exited = _container("exited", "bbb222")
    client.containers.list.return_value = [running, exited]

    with caplog.at_level(logging.INFO):
        assert backend.kill_run(RUN_ID) is True

    running.kill.assert_called_once_with()
    exited.kill.assert_not_called()
    running.remove.assert_called_once_with(force=True)
    exited.remove.assert_called_once_with(force=True)
    assert "already stopped (status=exited)" in caplog.text


def test_kill_run_tolerates_container_that_just_exited(backend, client, caplog):
    container = _container("running")
    container.kill.side_effect = docker.errors.APIError("container abc is not running")
    client.containers.list.return_value = [container]

    with caplog.at_level(logging.INFO):
        assert backend.kill_run(RUN_ID) is True

    assert "already exited" in caplog.text
    container.remove.assert_called_once_with(force=True)


def test_kill_run_logs_other_api_errors(backend, client, caplog):
    container = _container("running")
    container.kill.side_effect = docker.errors.APIError("daemon exploded")
    client.containers.list.return_value = [container]

    with caplog.at_level(logging.WARNING):
        assert backend.kill_run(RUN_ID) is True

    assert "Failed to kill container" in caplog.text
    container.remove.assert_called_once_with(force=True)


def test_kill_run_ignores_remove_failures(backend, client):
    container = _container("exited")
    container.remove.side_effect = docker.errors.APIError("already removed")
    client.containers.list.return_value = [container]

    assert backend.kill_run(RUN_ID) is True


# ── is_alive / logs ────────────────────────────────────────


def test_is_alive_filters_on_running_status(backend, client):
    client.containers.list.return_value = [_container()]
    assert backend.is_alive(RUN_ID) is True
    client.containers.list.assert_called_once_with(
        filters={"label": f"{CONTAINER_LABEL_PREFIX}={RUN_ID}", "status": "running"},
        all=True,
    )

    client.containers.list.return_value = []
    assert backend.is_alive(RUN_ID) is False


def test_get_container_logs(backend, client):
    client.containers.list.return_value = []
    assert backend.get_container_logs(RUN_ID) == f"[No container found for run {RUN_ID}]"

    container = _container()
    container.logs.return_value = b"line one\nline \xff two\n"
    client.containers.list.return_value = [container]
    assert backend.get_container_logs(RUN_ID, tail=7) == "line one\nline � two\n"
    container.logs.assert_called_once_with(tail=7, timestamps=True)

    container.logs.side_effect = docker.errors.APIError("gone")
    assert backend.get_container_logs(RUN_ID) == "[Failed to fetch container logs]"


# ── warm pool helpers ──────────────────────────────────────


def test_create_warm_container(backend, client, monkeypatch):
    monkeypatch.setattr(settings, "runner_image", "agentbox-runner:warm")
    client.containers.run.return_value = _container(container_id="warm123")

    assert backend.create_warm_container() == "warm123"
    kwargs = client.containers.run.call_args.kwargs
    assert kwargs["image"] == "agentbox-runner:warm"
    assert kwargs["labels"] == {"agentbox.warm": "true"}
    assert kwargs["command"] == ["sleep", "infinity"]
    assert kwargs["read_only"] is True

    client.containers.run.return_value = _container(container_id=None)
    assert backend.create_warm_container() == ""


def test_kill_by_id(backend, client, caplog):
    container = _container(container_id="warm123")
    client.containers.get.return_value = container

    backend.kill("warm123")
    container.kill.assert_called_once_with()
    container.remove.assert_called_once_with(force=True)

    client.containers.get.side_effect = docker.errors.NotFound("missing")
    backend.kill("missing-container")  # must not raise

    client.containers.get.side_effect = docker.errors.APIError("daemon down")
    with caplog.at_level(logging.ERROR):
        backend.kill("broken-container-id-that-is-long")
    assert "Failed to kill broken-conta" in caplog.text
