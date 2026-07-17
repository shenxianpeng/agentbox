"""Queue-claim tests for the launcher worker (requires Postgres).

Covers per-tenant concurrency limits: a tenant with max_concurrent=N never
has more than N runs claimed into 'running' at once.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio

from agentbox.db.migrate import migrate
from agentbox.db.queries import create_pool
from agentbox.launcher.worker import claim_next_run
from agentbox.settings import settings


@pytest_asyncio.fixture
async def pool():
    await migrate()
    pool = await create_pool(settings.database_url)
    yield pool
    await pool.close()


async def _create_tenant(pool, name: str, max_concurrent: int) -> str:
    tenant_id = str(uuid.uuid4())
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO tenants (id, name, max_concurrent) VALUES ($1::uuid, $2, $3)",
            tenant_id,
            name,
            max_concurrent,
        )
    return tenant_id


async def _enqueue_run(pool, tenant_id: str, agent_name: str) -> str:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO runs (tenant_id, agent_name, prompt)
            VALUES ($1::uuid, $2, 'test prompt')
            RETURNING id
            """,
            tenant_id,
            agent_name,
        )
    return str(row["id"])


async def _set_status(pool, run_id: str, status: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE runs SET status = $2 WHERE id = $1::uuid",
            run_id,
            status,
        )


@pytest.mark.asyncio
async def test_tenant_max_concurrent_enforced(pool):
    """A tenant with max_concurrent=1 gets no second claim while one runs."""
    tenant_id = await _create_tenant(pool, f"tenant-{uuid.uuid4().hex[:8]}", max_concurrent=1)
    run1 = await _enqueue_run(pool, tenant_id, "limit-test")
    run2 = await _enqueue_run(pool, tenant_id, "limit-test")

    claimed1 = await claim_next_run(pool, tenant_id)
    assert claimed1 is not None
    assert str(claimed1["id"]) == run1

    # Tenant is at its limit — nothing more to claim
    claimed2 = await claim_next_run(pool, tenant_id)
    assert claimed2 is None, "claim should respect tenants.max_concurrent"

    # Once the first run finishes, the second becomes claimable
    await _set_status(pool, run1, "succeeded")
    claimed3 = await claim_next_run(pool, tenant_id)
    assert claimed3 is not None
    assert str(claimed3["id"]) == run2


@pytest.mark.asyncio
async def test_tenant_limits_are_independent(pool):
    """One tenant at its limit does not block another tenant's queue."""
    tenant_a = await _create_tenant(pool, f"tenant-{uuid.uuid4().hex[:8]}", max_concurrent=1)
    tenant_b = await _create_tenant(pool, f"tenant-{uuid.uuid4().hex[:8]}", max_concurrent=1)

    await _enqueue_run(pool, tenant_a, "fairness-test")
    await _enqueue_run(pool, tenant_a, "fairness-test")
    run_b = await _enqueue_run(pool, tenant_b, "fairness-test")

    claimed_a = await claim_next_run(pool, tenant_a)
    assert claimed_a is not None

    # Tenant A is now at its limit, but tenant B can still claim
    assert await claim_next_run(pool, tenant_a) is None
    claimed_b = await claim_next_run(pool, tenant_b)
    assert claimed_b is not None
    assert str(claimed_b["id"]) == run_b
