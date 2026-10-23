"""Unit tests for the Kubernetes launcher backend.

The ``kubernetes`` client is mocked at the API-object level (``BatchV1Api`` /
``CoreV1Api``); no cluster is contacted. ``ApiException`` and the ``V1*``
model classes are the real ones so the error handling is exercised as it
would be against a live API server.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import kubernetes
import pytest
from kubernetes.client import ApiException

from agentbox.launcher.backend_k8s import NAMESPACE, K8sBackend
from agentbox.settings import settings

RUN_ID = "11111111-2222-3333-4444-555555555555"
SHORT = RUN_ID[:20]


@pytest.fixture
def k8s(monkeypatch) -> tuple[MagicMock, MagicMock]:
    """Mock the Kubernetes API clients and make in-cluster config succeed."""
    batch = MagicMock(name="BatchV1Api")
    core = MagicMock(name="CoreV1Api")
    monkeypatch.setattr(kubernetes.client, "BatchV1Api", lambda: batch)
    monkeypatch.setattr(kubernetes.client, "CoreV1Api", lambda: core)
    monkeypatch.setattr(kubernetes.config, "load_incluster_config", lambda: None)
    monkeypatch.setattr(kubernetes.config, "load_kube_config", MagicMock(name="load_kube_config"))
    return batch, core


@pytest.fixture
def backend(k8s) -> K8sBackend:
    return K8sBackend()


def _job(name: str = f"run-{SHORT}", uid: str = "job-uid-1") -> MagicMock:
    job = MagicMock(name="job")
    job.metadata.name = name
    job.metadata.uid = uid
    return job


# ── construction ───────────────────────────────────────────


def test_init_prefers_in_cluster_config(k8s):
    K8sBackend()
    kubernetes.config.load_kube_config.assert_not_called()


def test_init_falls_back_to_kubeconfig(k8s, monkeypatch):
    def _no_cluster():
        raise kubernetes.config.ConfigException("not in cluster")

    monkeypatch.setattr(kubernetes.config, "load_incluster_config", _no_cluster)
    K8sBackend()
    kubernetes.config.load_kube_config.assert_called_once_with()


# ── start_run ──────────────────────────────────────────────


def test_start_run_creates_secrets_and_job(backend, k8s, monkeypatch):
    batch, core = k8s
    monkeypatch.setattr(settings, "runner_image", "agentbox-runner:k8s")
    monkeypatch.setattr(settings, "model_name", "deepseek-chat")
    monkeypatch.setattr(settings, "logfire_token", "")
    monkeypatch.setattr(settings, "runtime_class", "")
    monkeypatch.setattr(settings, "credential_proxy_url", "http://credential-proxy:9090")
    batch.create_namespaced_job.return_value = _job()

    job_name = backend.start_run(
        run_id=RUN_ID,
        database_url="postgresql://agentbox:agentbox@postgres:5432/agentbox",
        scoped_credentials='{"llm:default": {"credential": "tok"}}',
    )

    assert job_name == f"run-{SHORT}"

    # Two Secrets: scoped credentials + DB/observability settings
    assert core.create_namespaced_secret.call_count == 2
    (ns1, creds_secret), (ns2, db_secret) = (
        c.args for c in core.create_namespaced_secret.call_args_list
    )
    assert ns1 == ns2 == NAMESPACE
    assert creds_secret.metadata.name == f"creds-{SHORT}"
    assert creds_secret.metadata.labels == {"agentbox.run_id": RUN_ID}
    assert creds_secret.string_data == {
        "credentials.json": '{"llm:default": {"credential": "tok"}}'
    }
    assert db_secret.metadata.name == f"db-creds-{SHORT}"
    assert db_secret.string_data == {
        "database_url": "postgresql://agentbox:agentbox@postgres:5432/agentbox",
        "logfire_token": "",
        "credential_proxy_url": "http://credential-proxy:9090",
    }

    # Job manifest is rendered from k8s/job-template.yaml
    ns, manifest = batch.create_namespaced_job.call_args.args
    assert ns == NAMESPACE
    assert manifest["metadata"]["name"] == f"run-{SHORT}"
    assert manifest["metadata"]["labels"] == {"agentbox.run_id": RUN_ID}
    pod_spec = manifest["spec"]["template"]["spec"]
    assert "runtimeClassName" not in pod_spec
    container = pod_spec["containers"][0]
    assert container["image"] == "agentbox-runner:k8s"
    env = {e["name"]: e for e in container["env"]}
    assert env["RUN_ID"]["value"] == RUN_ID
    assert env["MODEL_NAME"]["value"] == "deepseek-chat"
    assert env["TRACEPARENT"]["value"] == ""
    # Secrets are referenced, never inlined
    assert env["DATABASE_URL"]["valueFrom"]["secretKeyRef"]["name"] == f"db-creds-{RUN_ID}"
    assert env["AGENTBOX_CREDENTIALS_JSON"]["valueFrom"]["secretKeyRef"]["name"] == (
        f"creds-{RUN_ID}"
    )
    assert manifest["spec"]["backoffLimit"] == 0

    # Both Secrets are owned by the Job for garbage collection
    assert core.patch_namespaced_secret.call_count == 2
    for call, expected in zip(
        core.patch_namespaced_secret.call_args_list,
        [f"creds-{SHORT}", f"db-creds-{SHORT}"],
        strict=True,
    ):
        name, ns, body = call.args
        assert (name, ns) == (expected, NAMESPACE)
        assert body["metadata"]["ownerReferences"] == [
            {"apiVersion": "batch/v1", "kind": "Job", "name": f"run-{SHORT}", "uid": "job-uid-1"}
        ]


def test_start_run_with_runtime_class_and_overrides(backend, k8s, monkeypatch):
    batch, core = k8s
    monkeypatch.setattr(settings, "runtime_class", "gvisor")
    monkeypatch.setattr(settings, "logfire_token", "lf-token")
    batch.create_namespaced_job.return_value = _job()

    backend.start_run(
        run_id=RUN_ID,
        database_url="postgresql://x",
        scoped_credentials="{}",
        credential_proxy_url="http://proxy.override:1",
        traceparent="00-trace-span-01",
    )

    _, db_secret = core.create_namespaced_secret.call_args_list[1].args
    assert db_secret.string_data["credential_proxy_url"] == "http://proxy.override:1"
    assert db_secret.string_data["logfire_token"] == "lf-token"

    manifest = batch.create_namespaced_job.call_args.args[1]
    pod_spec = manifest["spec"]["template"]["spec"]
    assert pod_spec["runtimeClassName"] == "gvisor"
    env = {e["name"]: e for e in pod_spec["containers"][0]["env"]}
    assert env["TRACEPARENT"]["value"] == "00-trace-span-01"


def test_start_run_reuses_existing_secrets(backend, k8s, caplog):
    batch, core = k8s
    core.create_namespaced_secret.side_effect = ApiException(status=409, reason="Conflict")
    batch.create_namespaced_job.return_value = _job()

    with caplog.at_level(logging.WARNING):
        assert backend.start_run(RUN_ID, "postgresql://x", "{}") == f"run-{SHORT}"

    assert f"Secret creds-{SHORT} already exists" in caplog.text
    assert f"Secret db-creds-{SHORT} already exists" in caplog.text
    batch.create_namespaced_job.assert_called_once()


def test_start_run_propagates_credentials_secret_failure(backend, k8s):
    batch, core = k8s
    core.create_namespaced_secret.side_effect = ApiException(status=500, reason="boom")

    with pytest.raises(ApiException):
        backend.start_run(RUN_ID, "postgresql://x", "{}")

    core.create_namespaced_secret.assert_called_once()
    batch.create_namespaced_job.assert_not_called()


def test_start_run_cleans_up_when_db_secret_fails(backend, k8s):
    batch, core = k8s
    core.create_namespaced_secret.side_effect = [None, ApiException(status=500, reason="boom")]

    with pytest.raises(ApiException):
        backend.start_run(RUN_ID, "postgresql://x", "{}")

    core.delete_namespaced_secret.assert_called_once_with(f"creds-{SHORT}", NAMESPACE)
    batch.create_namespaced_job.assert_not_called()


def test_start_run_reuses_existing_job(backend, k8s, caplog):
    batch, core = k8s
    batch.create_namespaced_job.side_effect = ApiException(status=409, reason="Conflict")
    batch.read_namespaced_job.return_value = _job(uid="existing-uid")

    with caplog.at_level(logging.WARNING):
        assert backend.start_run(RUN_ID, "postgresql://x", "{}") == f"run-{SHORT}"

    assert f"Job run-{SHORT} already exists" in caplog.text
    batch.read_namespaced_job.assert_called_once_with(f"run-{SHORT}", NAMESPACE)
    owner = core.patch_namespaced_secret.call_args.args[2]["metadata"]["ownerReferences"][0]
    assert owner["uid"] == "existing-uid"


def test_start_run_cleans_up_both_secrets_when_job_fails(backend, k8s):
    batch, core = k8s
    batch.create_namespaced_job.side_effect = ApiException(status=403, reason="Forbidden")

    with pytest.raises(ApiException):
        backend.start_run(RUN_ID, "postgresql://x", "{}")

    deleted = [c.args[0] for c in core.delete_namespaced_secret.call_args_list]
    assert deleted == [f"creds-{SHORT}", f"db-creds-{SHORT}"]
    core.patch_namespaced_secret.assert_not_called()


def test_owner_reference_failure_is_logged_not_raised(backend, k8s, caplog):
    _, core = k8s
    core.patch_namespaced_secret.side_effect = ApiException(status=422, reason="Invalid")

    with caplog.at_level(logging.WARNING):
        backend._own_secrets_by_job(_job(), ["creds-x"])

    assert "Failed to set ownerReference on Secret creds-x" in caplog.text


# ── kill_run / is_alive ────────────────────────────────────


def test_kill_run_deletes_job_and_secrets(backend, k8s):
    batch, core = k8s

    assert backend.kill_run(RUN_ID) is True

    batch.delete_namespaced_job.assert_called_once_with(
        f"run-{SHORT}", NAMESPACE, propagation_policy="Background"
    )
    deleted = [c.args[0] for c in core.delete_namespaced_secret.call_args_list]
    assert deleted == [f"creds-{SHORT}", f"db-creds-{SHORT}"]


def test_kill_run_when_job_missing(backend, k8s):
    batch, core = k8s
    batch.delete_namespaced_job.side_effect = ApiException(status=404, reason="Not Found")

    assert backend.kill_run(RUN_ID) is False
    # Secrets are still cleaned up
    assert core.delete_namespaced_secret.call_count == 2


def test_kill_run_logs_other_job_errors(backend, k8s, caplog):
    batch, _ = k8s
    batch.delete_namespaced_job.side_effect = ApiException(status=500, reason="boom")

    with caplog.at_level(logging.WARNING):
        assert backend.kill_run(RUN_ID) is False

    assert f"Failed to delete Job run-{SHORT}" in caplog.text


def test_is_alive_true_when_job_active(backend, k8s):
    batch, core = k8s
    job = MagicMock()
    job.status.active = 1
    batch.read_namespaced_job.return_value = job

    assert backend.is_alive(RUN_ID) is True
    core.list_namespaced_pod.assert_not_called()


def test_is_alive_falls_back_to_running_pods(backend, k8s):
    batch, core = k8s
    job = MagicMock()
    job.status.active = 0
    batch.read_namespaced_job.return_value = job

    running = MagicMock()
    running.status.phase = "Running"
    pending = MagicMock()
    pending.status.phase = "Pending"
    core.list_namespaced_pod.return_value = MagicMock(items=[pending, running])
    assert backend.is_alive(RUN_ID) is True
    core.list_namespaced_pod.assert_called_with(
        NAMESPACE, label_selector=f"agentbox.run_id={RUN_ID}"
    )

    core.list_namespaced_pod.return_value = MagicMock(items=[pending])
    assert backend.is_alive(RUN_ID) is False

    core.list_namespaced_pod.return_value = MagicMock(items=None)
    assert backend.is_alive(RUN_ID) is False


def test_is_alive_handles_api_errors(backend, k8s):
    batch, _ = k8s
    batch.read_namespaced_job.side_effect = ApiException(status=404, reason="Not Found")
    assert backend.is_alive(RUN_ID) is False

    batch.read_namespaced_job.side_effect = ApiException(status=500, reason="boom")
    with pytest.raises(ApiException):
        backend.is_alive(RUN_ID)


# ── logs / warm pool / secrets ─────────────────────────────


def test_get_pod_logs(backend, k8s):
    _, core = k8s
    core.list_namespaced_pod.return_value = MagicMock(items=[])
    assert backend.get_pod_logs(RUN_ID) == f"[No pod found for run {RUN_ID}]"

    pod = MagicMock()
    pod.metadata.name = "run-pod-abc"
    core.list_namespaced_pod.return_value = MagicMock(items=[pod])
    core.read_namespaced_pod_log.return_value = "hello from pod"
    assert backend.get_pod_logs(RUN_ID, tail=9) == "hello from pod"
    core.read_namespaced_pod_log.assert_called_once_with("run-pod-abc", NAMESPACE, tail_lines=9)

    # A pod without metadata is still queried (empty name)
    nameless = MagicMock()
    nameless.metadata = None
    core.list_namespaced_pod.return_value = MagicMock(items=[nameless])
    backend.get_pod_logs(RUN_ID)
    assert core.read_namespaced_pod_log.call_args.args[0] == ""

    core.read_namespaced_pod_log.side_effect = ApiException(status=500, reason="boom")
    assert backend.get_pod_logs(RUN_ID).startswith("[Failed to get logs:")


def test_warm_pool_is_a_stub(backend, caplog):
    with caplog.at_level(logging.WARNING):
        warm_id = backend.create_warm_container()
    assert warm_id.startswith("warm-") and len(warm_id) == len("warm-") + 8
    assert "Warm pool not yet implemented" in caplog.text
    backend.kill(warm_id)  # no-op


def test_delete_secret(backend, k8s, caplog):
    _, core = k8s
    backend._delete_secret("creds-a")
    core.delete_namespaced_secret.assert_called_once_with("creds-a", NAMESPACE)

    core.delete_namespaced_secret.side_effect = ApiException(status=404, reason="Not Found")
    with caplog.at_level(logging.WARNING):
        backend._delete_secret("creds-missing")
    assert "creds-missing" not in caplog.text

    core.delete_namespaced_secret.side_effect = ApiException(status=500, reason="boom")
    with caplog.at_level(logging.WARNING):
        backend._delete_secret("creds-broken")
    assert "Failed to delete Secret creds-broken" in caplog.text
