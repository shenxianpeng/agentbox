"""Shared fixtures for the DB-backed tests (require Postgres via DATABASE_URL)."""

from __future__ import annotations

import pytest_asyncio

from agentbox.db.migrate import migrate
from agentbox.db.queries import create_pool
from agentbox.settings import settings


@pytest_asyncio.fixture
async def pool():
    """A migrated database and a fresh asyncpg pool per test."""
    await migrate()
    pool = await create_pool(settings.database_url)
    yield pool
    await pool.close()
