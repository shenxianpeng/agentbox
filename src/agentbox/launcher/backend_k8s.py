"""Kubernetes backend for the launcher — manages Jobs and Secrets via the K8s API."""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any

from agentbox.settings import settings

logger = logging.getLogger(__name__)

NAMESPACE = "agentbox"
PROXY_HOST = os.environ.get("EGRESS_PROXY_HOST", "egress-proxy")
PROXY_PORT = os.environ.get("EGRESS_PROXY_PORT", "8888")
PROXY_URL = f"http://{PROXY_HOST}:{PROXY_PORT}"


class K8sBackend:
    """Manages runner Jobs and Secrets via the Kubernetes API.

    Uses the ``kubernetes`` Python client. Requires in-cluster or kubeconfig
    authentication.
    """

    def __init__(self) -> None:
        import kubernetes as k8s

        try:
            k8s.config.load_incluster_config()
            logger.info("Loaded in-cluster Kubernetes config")
        except k8s.config.ConfigException:
            k8s.config.load_kube_config()
            logger.info("Loaded kubeconfig for Kubernetes client")

        self._batch = k8s.client.BatchV1Api()
        self._core = k8s.client.CoreV1Api()

    def start_run(
        self,
        run_id: str,
        database_url: str,
        scoped_credentials: str,
        env_overrides: dict[str, str] | None = None,
        credential_proxy_url: str = "",
        traceparent: str | None = None,
    ) -> str:
        """Start a runner Job for the given run.

        Creates temporary Secrets for:
          - Scoped credentials (creds-<RUN_ID>)
          - Database URL, Logfire token, credential proxy URL (db-creds-<RUN_ID>)
        Then creates the Job. All Secrets are cleaned up on kill or completion.
        Returns the Job name.
        """
        import kubernetes as k8s

        short_id = run_id[:20]
        secret_name = f"creds-{short_id}"
        db_secret_name = f"db-creds-{short_id}"
        job_name = f"run-{short_id}"

        # ── Create Secret with scoped credentials (per-run token) ──
        secret = k8s.client.V1Secret(
            metadata=k8s.client.V1ObjectMeta(
                name=secret_name,
                namespace=NAMESPACE,
                labels={"agentbox.run_id": run_id},
            ),
            string_data={"credentials.json": scoped_credentials},
        )
        try:
            self._core.create_namespaced_secret(NAMESPACE, secret)
            logger.info("Created Secret %s for run %s", secret_name, run_id)
        except k8s.client.ApiException as exc:
            if exc.status == 409:
                logger.warning("Secret %s already exists, reusing", secret_name)
            else:
                raise

        # ── Create Secret with DB credentials (not in plain env) ──
        db_secret = k8s.client.V1Secret(
            metadata=k8s.client.V1ObjectMeta(
                name=db_secret_name,
                namespace=NAMESPACE,
                labels={"agentbox.run_id": run_id},
            ),
            string_data={
                "database_url": database_url,
                "logfire_token": settings.logfire_token or "",
                "credential_proxy_url": credential_proxy_url or settings.credential_proxy_url,
            },
        )
        try:
            self._core.create_namespaced_secret(NAMESPACE, db_secret)
            logger.info("Created Secret %s for run %s", db_secret_name, run_id)
        except k8s.client.ApiException as exc:
            if exc.status == 409:
                logger.warning("Secret %s already exists, reusing", db_secret_name)
            else:
                self._delete_secret(secret_name)
                raise

        # ── Build the Job from template ──
        template_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "..", "k8s", "job-template.yaml"
        )
        with open(template_path) as f:
            template = f.read()

        replacements = {
            "<RUN_ID>": run_id,
            "<MODEL_NAME>": settings.model_name,
            "<TRACEPARENT>": traceparent or "",
            "<RUNNER_IMAGE>": settings.runner_image,
            "<PROXY_URL>": PROXY_URL,
            "<RUNTIME_CLASS>": (
                f"      runtimeClassName: {settings.runtime_class}"
                if settings.runtime_class
                else ""
            ),
        }
        for key, value in replacements.items():
            template = template.replace(key, value)

        import yaml

        job_manifest = yaml.safe_load(template)
        job_manifest["metadata"]["name"] = job_name

        try:
            job = self._batch.create_namespaced_job(NAMESPACE, job_manifest)
            logger.info("Created Job %s for run %s", job_name, run_id)
        except k8s.client.ApiException as exc:
            if exc.status == 409:
                logger.warning("Job %s already exists", job_name)
                job = self._batch.read_namespaced_job(job_name, NAMESPACE)
            else:
                self._delete_secret(secret_name)
                self._delete_secret(db_secret_name)
                raise

        # ── Own the Secrets by the Job so Kubernetes garbage-collects them ──
        # ttlSecondsAfterFinished deletes the Job after completion; without an
        # ownerReference the Secrets of successful runs would leak (the reaper
        # only cleans up runs whose lease died).
        self._own_secrets_by_job(job, [secret_name, db_secret_name])

        return job_name

    def _own_secrets_by_job(self, job: Any, secret_names: list[str]) -> None:
        """Patch Secrets with an ownerReference to the Job for cascade deletion."""
        import kubernetes as k8s

        # Raw patch body uses Kubernetes API (camelCase) field names
        owner_ref = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "name": job.metadata.name,
            "uid": job.metadata.uid,
        }
        for name in secret_names:
            try:
                self._core.patch_namespaced_secret(
                    name,
                    NAMESPACE,
                    {"metadata": {"ownerReferences": [owner_ref]}},
                )
            except k8s.client.ApiException as exc:
                logger.warning("Failed to set ownerReference on Secret %s: %s", name, exc)

    def kill_run(self, run_id: str) -> bool:
        """Delete the Job and Secrets for the given run.

        Returns True if a Job was found and deleted, False otherwise.
        """
        import kubernetes as k8s

        short_id = run_id[:20]
        job_name = f"run-{short_id}"

        found = False

        # Delete the Job
        try:
            self._batch.delete_namespaced_job(
                job_name,
                NAMESPACE,
                propagation_policy="Background",
            )
            logger.info("Deleted Job %s for run %s", job_name, run_id)
            found = True
        except k8s.client.ApiException as exc:
            if exc.status != 404:
                logger.warning("Failed to delete Job %s: %s", job_name, exc)

        # Clean up all Secrets for this run
        self._delete_secret(f"creds-{short_id}")
        self._delete_secret(f"db-creds-{short_id}")

        return found

    def is_alive(self, run_id: str) -> bool:
        """Check if the Job for a run is still running."""
        import kubernetes as k8s

        job_name = f"run-{run_id[:20]}"
        try:
            job = self._batch.read_namespaced_job(job_name, NAMESPACE)
            if job.status and job.status.active and job.status.active > 0:
                return True
            # Check pods as well
            pods = self._core.list_namespaced_pod(
                NAMESPACE,
                label_selector=f"agentbox.run_id={run_id}",
            )
            return any(pod.status and pod.status.phase == "Running" for pod in pods.items)
        except k8s.client.ApiException as exc:
            if exc.status == 404:
                return False
            raise

    def get_pod_logs(self, run_id: str, tail: int = 50) -> str:
        """Get recent logs from a run's pod."""
        import kubernetes as k8s

        try:
            pods = self._core.list_namespaced_pod(
                NAMESPACE,
                label_selector=f"agentbox.run_id={run_id}",
            )
            if not pods.items:
                return f"[No pod found for run {run_id}]"
            pod_name = pods.items[0].metadata.name
            logs = self._core.read_namespaced_pod_log(
                pod_name,
                NAMESPACE,
                tail_lines=tail,
            )
            return logs
        except k8s.client.ApiException as exc:
            return f"[Failed to get logs: {exc}]"

    def create_warm_container(self) -> str:
        """Create a placeholder Job for warm pool (not implemented for K8s).

        Returns a dummy ID. In production, this would pre-pull the image
        on cluster nodes via a DaemonSet.
        """
        logger.warning("Warm pool not yet implemented for K8s backend")
        return f"warm-{uuid.uuid4().hex[:8]}"

    def kill(self, container_id: str) -> None:
        """Kill a warm container (no-op for K8s)."""
        pass

    def _delete_secret(self, secret_name: str) -> None:
        """Delete a Secret by name."""
        import kubernetes as k8s

        try:
            self._core.delete_namespaced_secret(secret_name, NAMESPACE)
            logger.info("Deleted Secret %s", secret_name)
        except k8s.client.ApiException as exc:
            if exc.status != 404:
                logger.warning("Failed to delete Secret %s: %s", secret_name, exc)
