"""Egress-control end-to-end tests (requires the compose stack).

Verifies the default-deny egress story from inside the sandbox network:

  1. A container on agentbox-internal has no direct internet access.
  2. Through the egress proxy, a non-allowlisted domain is DENIED.
  3. Through the egress proxy, an allowlisted domain (LLM API) is allowed.

Requirements: Docker daemon running and the compose stack up
(``docker compose up -d`` — needs the egress-proxy service and the
``agentbox-internal`` network). Skips itself otherwise.
"""

from __future__ import annotations

import pytest

INTERNAL_NETWORK = "agentbox-internal"
PROXY_URL = "http://egress-proxy:8888"
CURL_IMAGE = "curlimages/curl:8.7.1"

ALLOWED_URL = "https://api.deepseek.com/"
DENIED_URL = "https://example.com/"


def _docker_client():
    try:
        import docker

        client = docker.from_env()
        client.ping()
        return client
    except Exception:
        return None


def _stack_ready(client) -> bool:
    try:
        client.networks.get(INTERNAL_NETWORK)
    except Exception:
        return False
    proxies = client.containers.list(filters={"status": "running"})
    return any("egress-proxy" in (c.name or "") for c in proxies)


def _curl_via_proxy(client, url: str) -> tuple[int, str]:
    """Run curl in a sandbox-network container through the egress proxy.

    Returns (exit_code, combined output). curl exits 56 with a
    "CONNECT tunnel failed, response 403" message when the proxy denies
    the destination.
    """
    import docker

    try:
        output = client.containers.run(
            CURL_IMAGE,
            [
                "-sS",
                "-o",
                "/dev/null",
                "-w",
                "%{http_code}",
                "--max-time",
                "20",
                "-x",
                PROXY_URL,
                url,
            ],
            network=INTERNAL_NETWORK,
            remove=True,
            stderr=True,
        )
        return 0, output.decode(errors="replace")
    except docker.errors.ContainerError as exc:
        combined = (exc.stderr or b"").decode(errors="replace")
        return exc.exit_status, combined


def _curl_direct(client, url: str) -> tuple[int, str]:
    """Run curl in the sandbox network WITHOUT the proxy (must fail)."""
    import docker

    try:
        output = client.containers.run(
            CURL_IMAGE,
            ["-sS", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "10", url],
            network=INTERNAL_NETWORK,
            remove=True,
            stderr=True,
        )
        return 0, output.decode(errors="replace")
    except docker.errors.ContainerError as exc:
        combined = (exc.stderr or b"").decode(errors="replace")
        return exc.exit_status, combined


@pytest.fixture(scope="module")
def docker_stack():
    client = _docker_client()
    if client is None:
        pytest.skip("Docker daemon not available")
    if not _stack_ready(client):
        pytest.skip("Compose stack not running (need egress-proxy + agentbox-internal network)")
    try:
        client.images.pull(CURL_IMAGE)
    except Exception:
        pytest.skip(f"Cannot pull {CURL_IMAGE} (no registry access)")
    return client


def test_no_direct_internet_from_sandbox_network(docker_stack):
    """agentbox-internal is internal: direct egress must fail entirely."""
    exit_code, output = _curl_direct(docker_stack, DENIED_URL)
    assert exit_code != 0, (
        f"Direct internet access from the sandbox network should fail, "
        f"but curl succeeded: {output!r}"
    )


def test_proxy_denies_non_allowlisted_domain(docker_stack):
    """The egress proxy must deny domains outside the allowlist."""
    exit_code, output = _curl_via_proxy(docker_stack, DENIED_URL)
    assert exit_code != 0 and "403" in output, (
        f"Expected the proxy to deny {DENIED_URL} with 403, got exit={exit_code}, output={output!r}"
    )


def test_proxy_allows_allowlisted_domain(docker_stack):
    """The egress proxy must pass through allowlisted LLM API domains."""
    exit_code, output = _curl_via_proxy(docker_stack, ALLOWED_URL)
    # Any real HTTP status (200/401/404/...) means the CONNECT tunnel was
    # allowed and TLS reached the upstream — the point is it wasn't blocked.
    assert exit_code == 0, (
        f"Expected the proxy to allow {ALLOWED_URL}, got exit={exit_code}, output={output!r}"
    )
    assert output.strip() != "000"
