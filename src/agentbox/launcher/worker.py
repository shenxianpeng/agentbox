"""Launcher worker — polls the queue and manages sandbox containers.

The launcher:
  1. Polls for queued runs (FOR UPDATE SKIP LOCKED per tenant)
  2. Claims a run and creates a lease
  3. Starts a sandbox container via the Docker backend
  4. Reaps dead containers (leases older than 30s with no heartbeat)
  5. Requeues or fails runs based on attempt count

This is the "resume mechanism": if a container is killed, the lease expires,
the reaper finds it, kills the container, and sets the run back to 'queued'.
The run will be picked up again and fast-forward through completed checkpoints.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

import asyncpg
import logfire

from agentbox.db.queries import create_pool
from agentbox.settings import settings

logger = logging.getLogger(__name__)

POLL_INTERVAL = 2  # seconds between queue polls
REAPER_INTERVAL = 10  # seconds between reaper scans
LEASE_TTL_SECONDS = 30  # lease considered dead after this long
# Use settings.max_concurrent_runs instead of this constant
# MAX_CONCURRENT_RUNS = 3


async def get_tenant_ids(pool: asyncpg.Pool) -> list[str]:
    """Get all tenant IDs for round-robin scheduling."""
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT id FROM tenants ORDER BY created_at ASC")
    return [str(r["id"]) for r in rows]


async def claim_next_run(
    pool: asyncpg.Pool,
    tenant_id: str | None = None,
) -> dict | None:
    """Claim the next queued run for a tenant (or any tenant)."""
    if tenant_id is None:
        tenant_id = "00000000-0000-0000-0000-000000000001"

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE runs
            SET status = 'running', attempt = attempt + 1, started_at = now()
            WHERE id = (
                SELECT id FROM runs
                WHERE status = 'queued' AND tenant_id = $1::uuid
                ORDER BY created_at ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            RETURNING id, tenant_id, agent_name, prompt, egress_allow, attempt, max_attempts
            """,
            tenant_id,
        )
    return dict(row) if row else None


async def create_lease(pool: asyncpg.Pool, run_id: str) -> str:
    """Create a lease for a run. Returns the owner ID."""
    owner = f"launcher-{uuid.uuid4().hex[:8]}"
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO leases (run_id, owner, heartbeat_at)
            VALUES ($1::uuid, $2, now())
            ON CONFLICT (run_id) DO UPDATE
            SET owner = $2, heartbeat_at = now()
            """,
            run_id,
            owner,
        )
    return owner


async def release_lease(pool: asyncpg.Pool, run_id: str) -> None:
    """Delete a lease for a run."""
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM leases WHERE run_id = $1::uuid",
            run_id,
        )


async def fail_run(pool: asyncpg.Pool, run_id: str, error: str) -> None:
    """Mark a run as failed permanently."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE runs
            SET status = 'failed', error = $2, finished_at = now()
            WHERE id = $1::uuid
            """,
            run_id,
            error,
        )


async def requeue_run(pool: asyncpg.Pool, run_id: str) -> None:
    """Set a run back to queued for retry."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE runs
            SET status = 'queued', started_at = NULL
            WHERE id = $1::uuid
            """,
            run_id,
        )


async def get_dead_leases(pool: asyncpg.Pool) -> list[dict]:
    """Find leases that haven't been heartbeated within the TTL.

    Only considers runs that are still in 'running' status.
    Runs that succeeded or failed will have their leases deleted by the runner.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT l.run_id, l.owner, l.heartbeat_at, r.attempt, r.max_attempts
            FROM leases l
            JOIN runs r ON r.id = l.run_id
            WHERE l.heartbeat_at < now() - make_interval(secs => $1)
              AND r.status = 'running'
            """,
            LEASE_TTL_SECONDS,
        )
    return [dict(r) for r in rows]


async def get_running_count(pool: asyncpg.Pool) -> int:
    """Count currently running runs."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT COUNT(*) as cnt FROM runs WHERE status = 'running'")
    return row["cnt"] if row else 0


async def launcher_loop(pool: asyncpg.Pool, backend: Any) -> None:
    """Main launcher loop: poll queue and start containers."""
    logger.info(
        "Launcher started (poll=%ds, reaper=%ds, max_concurrent=%d)",
        POLL_INTERVAL,
        REAPER_INTERVAL,
        settings.max_concurrent_runs,
    )

    tenant_round_robin_index = 0

    while True:
        try:
            running_count = await get_running_count(pool)
            if running_count < settings.max_concurrent_runs:
                # Round-robin across tenants for fairness
                tenants = await get_tenant_ids(pool)
                if not tenants:
                    tenants = ["00000000-0000-0000-0000-000000000001"]

                claimed = False
                for _ in range(len(tenants)):
                    tenant_id = tenants[tenant_round_robin_index % len(tenants)]
                    tenant_round_robin_index += 1

                    run = await claim_next_run(pool, tenant_id)
                    if run:
                        await _handle_claimed_run(pool, backend, run)
                        claimed = True
                        break

                if not claimed:
                    logger.debug("No queued runs found")
            else:
                logger.debug("At max concurrent runs (%d), waiting...", settings.max_concurrent_runs)

        except Exception as exc:
            logger.exception("Error in launcher poll loop: %s", exc)

        await asyncio.sleep(POLL_INTERVAL)


async def _fetch_scoped_credentials(pool: asyncpg.Pool, run_id: str) -> dict[str, Any]:
    """Fetch scoped credentials from DB and format them for the runner.

    The stored credential is a per-run token (UUID), NOT the real API key.
    The real key is registered with the credential proxy by _register_with_credential_proxy().
    """
    async with pool.acquire() as conn:
        cred_rows = await conn.fetch(
            "SELECT scope, credential, expires_at FROM scoped_credentials WHERE run_id = $1::uuid",
            run_id,
        )

    return {
        f"llm:{row['scope'].replace('llm:', '')}": {
            "credential": row["credential"],
            "expires_at": (
                row["expires_at"].isoformat()
                if hasattr(row["expires_at"], "isoformat")
                else str(row["expires_at"])
            ),
        }
        for row in cred_rows
    }


def _get_llm_api_key_and_base_url() -> tuple[str, str] | None:
    """Get the real LLM API key and base URL from settings.

    Returns (api_key, base_url) or None if no API key is configured.
    The real key is registered with the credential proxy; it NEVER enters the sandbox.
    """
    if settings.deepseek_api_key:
        return settings.deepseek_api_key, "https://api.deepseek.com"
    if settings.anthropic_api_key:
        return settings.anthropic_api_key, "https://api.anthropic.com"
    return None


async def _register_with_credential_proxy(
    per_run_token: str,
    real_api_key: str,
    base_url: str,
) -> None:
    """Register the per-run token → real API key mapping with the credential proxy.

    After this call, the credential proxy can accept requests from the sandbox
    bearing the per-run token and inject the real API key before forwarding.
    """
    proxy_url = settings.credential_proxy_url
    import httpx

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"{proxy_url}/admin/keys",
                json={
                    "run_token": per_run_token,
                    "api_key": real_api_key,
                    "base_url": base_url,
                },
                headers={"Authorization": f"Bearer {settings.agentbox_api_token}"},
            )
            if resp.status_code != 200:
                logger.error(
                    "Failed to register key with credential proxy: %s %s",
                    resp.status_code,
                    resp.text,
                )
            else:
                logger.info(
                    "Registered key with credential proxy (token prefix: %s...)",
                    per_run_token[:8],
                )
    except httpx.RequestError as exc:
        logger.warning("Credential proxy unavailable: %s (runs will still work)", exc)


async def _unregister_from_credential_proxy(per_run_token: str) -> None:
    """Remove the key mapping from the credential proxy (run finished/failed)."""
    proxy_url = settings.credential_proxy_url
    import httpx

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.delete(
                f"{proxy_url}/admin/keys/{per_run_token}",
                headers={"Authorization": f"Bearer {settings.agentbox_api_token}"},
            )
    except httpx.RequestError:
        pass


async def _handle_claimed_run(pool: asyncpg.Pool, backend: Any, run: dict) -> None:
    """Handle a claimed run: create lease, register credentials with proxy, start container.

    The master API key is registered with the credential proxy and NEVER
    enters the sandbox container. The sandbox only gets a per-run token.
    """
    run_id = str(run["id"])
    logger.info(
        "Claimed run %s (agent=%s, attempt=%d/%d)",
        run_id,
        run["agent_name"],
        run["attempt"],
        run["max_attempts"],
    )

    with logfire.span(
        "launcher-claim",
        run_id=run_id,
        agent=run["agent_name"],
        attempt=run["attempt"],
    ):
        await create_lease(pool, run_id)

        # Fetch scoped credentials (per-run token) from DB
        creds = await _fetch_scoped_credentials(pool, run_id)

        # Register the real API key with the credential proxy
        key_info = _get_llm_api_key_and_base_url()
        if key_info:
            real_api_key, base_url = key_info
            # Get the first per-run token from credentials
            for scope_key, cred_info in creds.items():
                per_run_token = cred_info["credential"]
                await _register_with_credential_proxy(per_run_token, real_api_key, base_url)
                break
        else:
            logger.warning("No LLM API key configured — runs will fail")

        try:
            backend.start_run(
                run_id=run_id,
                database_url=settings.database_url,
                scoped_credentials=json.dumps(creds),
                credential_proxy_url=settings.credential_proxy_url,
            )
        except Exception as exc:
            logger.exception("Failed to start container for run %s: %s", run_id, exc)
            # Clean up the key mapping
            for scope_key, cred_info in creds.items():
                await _unregister_from_credential_proxy(cred_info["credential"])
            await release_lease(pool, run_id)
            if run["attempt"] >= run["max_attempts"]:
                await fail_run(pool, run_id, f"Container start failed: {exc}")
            else:
                await requeue_run(pool, run_id)


async def reaper_loop(pool: asyncpg.Pool, backend: Any) -> None:
    """Reaper loop: find dead leases and clean up."""
    while True:
        try:
            dead = await get_dead_leases(pool)
            for lease in dead:
                run_id = str(lease["run_id"])
                attempt = lease["attempt"]
                max_attempts = lease["max_attempts"]

                logger.warning(
                    "Reaping dead lease for run %s (attempt %d/%d, heartbeat=%s)",
                    run_id,
                    attempt,
                    max_attempts,
                    lease["heartbeat_at"],
                )

                with logfire.span(
                    "launcher-reap",
                    run_id=run_id,
                    attempt=attempt,
                    max_attempts=max_attempts,
                ):
                    # Kill the container
                    try:
                        backend.kill_run(run_id)
                    except Exception as exc:
                        logger.warning("Failed to kill container for run %s: %s", run_id, exc)

                    # Release lease
                    await release_lease(pool, run_id)

                    # Requeue or fail
                    if attempt >= max_attempts:
                        await fail_run(
                            pool,
                            run_id,
                            f"Run exceeded max attempts ({max_attempts}). "
                            f"Last lease heartbeat: {lease['heartbeat_at']}",
                        )
                        logfire.info("Run failed permanently (max attempts)", run_id=run_id)
                    else:
                        await requeue_run(pool, run_id)
                        logfire.info("Run requeued for retry", run_id=run_id, attempt=attempt)

        except Exception as exc:
            logger.exception("Error in reaper loop: %s", exc)

        await asyncio.sleep(REAPER_INTERVAL)


async def main() -> None:
    """Run both the launcher and reaper loops."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    logger.info("Initializing launcher (backend=%s)...", settings.agentbox_backend)

    pool = await create_pool(settings.database_url)

    # Select backend based on configuration
    if settings.agentbox_backend == "k8s":
        from agentbox.launcher.backend_k8s import K8sBackend

        backend = K8sBackend()
    else:
        from agentbox.launcher.backend_docker import DockerBackend

        backend = DockerBackend()

    # Run both loops concurrently
    await asyncio.gather(
        launcher_loop(pool, backend),
        reaper_loop(pool, backend),
    )


if __name__ == "__main__":
    asyncio.run(main())
