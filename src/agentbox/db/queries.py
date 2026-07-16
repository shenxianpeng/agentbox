"""Database queries for the control plane API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import asyncpg


async def create_pool(database_url: str) -> asyncpg.Pool:
    """Create a reusable asyncpg connection pool."""
    return await asyncpg.create_pool(
        database_url,
        min_size=2,
        max_size=10,
    )


# ── Runs ────────────────────────────────────────────────────


async def insert_run(
    pool: asyncpg.Pool,
    agent_name: str,
    prompt: str,
    egress_allow: list[str] | None = None,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """Insert a new queued run and return its row as a dict."""
    run_id = uuid.uuid4()
    if tenant_id is None:
        tenant_id = "00000000-0000-0000-0000-000000000001"
    if egress_allow is None:
        egress_allow = []

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO runs (id, tenant_id, agent_name, prompt, egress_allow)
            VALUES ($1, $2::uuid, $3, $4, $5)
            RETURNING id, status, tenant_id, agent_name, prompt, egress_allow,
                      attempt, max_attempts, created_at, started_at, finished_at,
                      result, error, cost_estimate
            """,
            run_id,
            tenant_id,
            agent_name,
            prompt,
            egress_allow,
        )
    return dict(row)


async def get_run(pool: asyncpg.Pool, run_id: str) -> dict[str, Any] | None:
    """Fetch a single run by ID."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, status, tenant_id, agent_name, prompt, egress_allow,
                   attempt, max_attempts, created_at, started_at, finished_at,
                   result, error, cost_estimate
            FROM runs
            WHERE id = $1::uuid
            """,
            run_id,
        )
    return dict(row) if row else None


# ── Checkpoints ─────────────────────────────────────────────


async def get_checkpoints(pool: asyncpg.Pool, run_id: str) -> list[dict[str, Any]]:
    """Fetch checkpoints for a run (without full payload)."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT step_index, kind, token_count, cost, created_at
            FROM checkpoints
            WHERE run_id = $1::uuid
            ORDER BY step_index ASC
            """,
            run_id,
        )
    return [dict(r) for r in rows]


# ── Scoped Credentials ──────────────────────────────────────


async def insert_scoped_credential(
    pool: asyncpg.Pool,
    run_id: str,
    credential: str,
    scope: str,
    expires_at: datetime,
) -> dict[str, Any]:
    """Store a scoped credential for a run."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO scoped_credentials (run_id, credential, scope, expires_at)
            VALUES ($1::uuid, $2, $3, $4)
            RETURNING id, run_id, scope, expires_at, created_at
            """,
            run_id,
            credential,
            scope,
            expires_at,
        )
    return dict(row)


async def cancel_run(pool: asyncpg.Pool, run_id: str) -> dict[str, Any] | None:
    """Cancel a run by setting status to 'canceled' and returning finished_at.

    Only cancels runs that are 'queued' or 'running' (not already finished).
    Returns the updated run row, or None if not found or already finished.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE runs
            SET status = 'canceled', finished_at = now(), error = 'Canceled by user'
            WHERE id = $1::uuid
              AND status IN ('queued', 'running')
            RETURNING id, status, tenant_id, agent_name, prompt, egress_allow,
                      attempt, max_attempts, created_at, started_at, finished_at,
                      result, error, cost_estimate
            """,
            run_id,
        )
    return dict(row) if row else None


async def get_scoped_credentials(pool: asyncpg.Pool, run_id: str) -> list[dict[str, Any]]:
    """Fetch all scoped credentials for a run."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, scope, expires_at, created_at
            FROM scoped_credentials
            WHERE run_id = $1::uuid
            ORDER BY created_at ASC
            """,
            run_id,
        )
    return [dict(r) for r in rows]
