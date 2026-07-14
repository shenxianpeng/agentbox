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
    ) -> str:
        """Start a runner container for the given run.

        Returns the container ID.
        """
        proxy_host = os.environ.get("EGRESS_PROXY_HOST", "egress-proxy")
        proxy_port = os.environ.get("EGRESS_PROXY_PORT", "8888")
        proxy_url = f"http://{proxy_host}:{proxy_port}"

        env = {
            "RUN_ID": run_id,
            "DATABASE_URL": database_url,
            "AGENTBOX_CREDENTIALS_JSON": scoped_credentials,
            "MODEL_NAME": settings.model_name,
            "LOGFIRE_TOKEN": settings.logfire_token,
            "PYTHONUNBUFFERED": "1",
            "HTTP_PROXY": proxy_url,
            "HTTPS_PROXY": proxy_url,
            "NO_PROXY": "localhost,127.0.0.1,0.0.0.0",
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
        return container.id

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
                container.kill()
                logger.info("Killed container %s for run %s", container.short_id, run_id)
            except docker.errors.APIError:
                logger.exception(
                    "Failed to kill container %s for run %s", container.short_id, run_id
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
