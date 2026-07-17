"""Docker backend for the launcher — manages sandbox containers via Docker SDK."""

from __future__ import annotations

import logging
import os

from docker.models.containers import Container

import docker
from agentbox.settings import settings

logger = logging.getLogger(__name__)

CONTAINER_LABEL_PREFIX = "agentbox.run_id"
NETWORK_NAME = "agentbox-internal"


class DockerBackend:
    """Manages runner containers via the Docker SDK."""

    def __init__(self) -> None:
        self._client = docker.from_env()

    def start_run(
        self,
        run_id: str,
        database_url: str,
        scoped_credentials: str,
        env_overrides: dict[str, str] | None = None,
        credential_proxy_url: str = "",
        traceparent: str | None = None,
    ) -> str:
        """Start a runner container for the given run.

        Args:
            run_id: The run UUID.
            database_url: Postgres connection string for checkpoint writes.
            scoped_credentials: JSON with per-run tokens (NOT real API keys).
            env_overrides: Optional extra environment variables.
            credential_proxy_url: URL of the credential proxy for LLM API calls.

        Returns the container ID.
        """
        proxy_host = os.environ.get("EGRESS_PROXY_HOST", "egress-proxy")
        proxy_port = os.environ.get("EGRESS_PROXY_PORT", "8888")
        proxy_url = f"http://{proxy_host}:{proxy_port}"

        # When running inside a Docker Compose network, replace localhost
        # with the postgres service hostname so the runner can reach the DB.
        runner_db_url = settings.runner_database_url.replace("@localhost:", "@postgres:")

        env = {
            "RUN_ID": run_id,
            # pydantic-settings reads 'runner_database_url' from env var RUNNER_DATABASE_URL
            "RUNNER_DATABASE_URL": runner_db_url,
            "AGENTBOX_CREDENTIALS_JSON": scoped_credentials,
            "MODEL_NAME": settings.model_name,
            "LOGFIRE_TOKEN": settings.logfire_token,
            "PYTHONUNBUFFERED": "1",
            "CREDENTIAL_PROXY_URL": credential_proxy_url,
            # W3C trace context: the runner attaches this so its spans join
            # the trace started by POST /runs
            "TRACEPARENT": traceparent or "",
            "HTTP_PROXY": proxy_url,
            "HTTPS_PROXY": proxy_url,
            "NO_PROXY": "localhost,127.0.0.1,0.0.0.0,postgres,credential-proxy",
            # postgres and credential-proxy use raw TCP, not HTTP
        }
        if env_overrides:
            env.update(env_overrides)

        container: Container = self._client.containers.run(
            image=settings.runner_image,
            environment=env,
            labels={CONTAINER_LABEL_PREFIX: run_id},
            detach=True,
            network=NETWORK_NAME,
            cpu_period=100000,
            cpu_quota=100000,  # 1 CPU
            mem_limit="512m",
            pids_limit=100,
            read_only=True,
            tmpfs={"/tmp": ""},  # writable /tmp for Python temp files, SSL certs, etc.
            cap_add=[],
            extra_hosts={
                "host.docker.internal": "host-gateway",
            },
        )
        logger.info(
            "Started container %s for run %s (image: %s)",
            container.short_id,
            run_id,
            settings.runner_image,
        )
        return container.id or ""

    def kill_run(self, run_id: str) -> bool:
        """Kill and remove the container for the given run.

        Returns True if a container was found and killed, False otherwise.
        """
        containers = self._find_containers(run_id)
        if not containers:
            logger.warning("No container found for run %s", run_id)
            return False

        for container in containers:
            try:
                if container.status == "running":
                    container.kill()
                    logger.info("Killed container %s for run %s", container.short_id, run_id)
                else:
                    logger.info(
                        "Container %s already stopped (status=%s) for run %s — removing",
                        container.short_id,
                        container.status,
                        run_id,
                    )
            except docker.errors.APIError as exc:
                if "is not running" in str(exc):
                    logger.info(
                        "Container %s already exited for run %s — removing",
                        container.short_id,
                        run_id,
                    )
                else:
                    logger.warning(
                        "Failed to kill container %s for run %s: %s",
                        container.short_id,
                        run_id,
                        exc,
                    )

        # Remove after kill
        for container in containers:
            try:
                container.remove(force=True)
            except docker.errors.APIError:
                pass

        return True

    def is_alive(self, run_id: str) -> bool:
        """Check if the container for a run is still running."""
        containers = self._find_containers(run_id, status="running")
        return len(containers) > 0

    def get_container_logs(self, run_id: str, tail: int = 50) -> str:
        """Get recent logs from a run's container."""
        containers = self._find_containers(run_id)
        if not containers:
            return f"[No container found for run {run_id}]"
        try:
            logs = containers[0].logs(tail=tail, timestamps=True)
            return logs.decode("utf-8", errors="replace")
        except docker.errors.APIError:
            return "[Failed to fetch container logs]"

    def create_warm_container(self) -> str:
        """Create a pre-initialized container for the warm pool."""
        container: Container = self._client.containers.run(
            image=settings.runner_image,
            labels={"agentbox.warm": "true"},
            detach=True,
            network=NETWORK_NAME,
            cpu_period=100000,
            cpu_quota=100000,
            mem_limit="512m",
            pids_limit=100,
            read_only=True,
            cap_add=[],
            command=["sleep", "infinity"],
        )
        logger.info("Created warm container %s", container.short_id)
        return container.id or ""

    def kill(self, container_id: str) -> None:
        """Kill a container by ID (for warm pool cleanup)."""
        try:
            c = self._client.containers.get(container_id)
            c.kill()
            c.remove(force=True)
        except docker.errors.NotFound:
            pass
        except docker.errors.APIError:
            logger.exception("Failed to kill %s", container_id[:12])

    def _find_containers(
        self,
        run_id: str,
        status: str | None = None,
    ) -> list[Container]:
        """Find containers by run_id label."""
        filters = {"label": f"{CONTAINER_LABEL_PREFIX}={run_id}"}
        if status:
            filters["status"] = status
        return self._client.containers.list(filters=filters, all=True)
