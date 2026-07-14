"""End-to-end tests for the control plane API.

These tests require a running Postgres instance.
Set DATABASE_URL env var or the default local one will be used.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from agentbox.api.main import app
from agentbox.db.migrate import migrate
from agentbox.db.queries import create_pool
from agentbox.settings import settings


@pytest_asyncio.fixture
async def client():
    """Set up the app with a fresh database pool per test."""
    # Apply migrations once (idempotent)
    await migrate()

    # Create a fresh pool for this test
    pool = await create_pool(settings.database_url)
    app.state.pool = pool

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {settings.agentbox_api_token}"},
    ) as ac:
        yield ac

    # Cleanup
    await pool.close()


@pytest.mark.asyncio
async def test_create_and_read_run(client: AsyncClient):
    """POST /runs → returns 201 with queued status and scoped credentials."""
    response = await client.post(
        "/runs",
        json={
            "agent_name": "test-agent",
            "prompt": "Analyze the system for incidents.",
        },
    )

    assert response.status_code == 201, f"Expected 201, got {response.status_code}: {response.text}"
    data = response.json()

    # Check run fields
    assert data["status"] == "queued"
    assert data["agent_name"] == "test-agent"
    assert data["prompt"] == "Analyze the system for incidents."
    assert "id" in data
    assert data["attempt"] == 0
    assert data["max_attempts"] == 3

    # Check scoped credentials are present
    assert "credentials" in data
    assert len(data["credentials"]) > 0
    cred = data["credentials"][0]
    assert "scope" in cred
    assert "expires_at" in cred
    # The credential VALUE should NOT be in the response
    assert "credential" not in cred

    # Read run back via GET
    run_id = data["id"]
    get_resp = await client.get(f"/runs/{run_id}")
    assert get_resp.status_code == 200
    get_data = get_resp.json()
    assert get_data["id"] == run_id
    assert get_data["status"] == "queued"
    assert get_data["agent_name"] == "test-agent"


@pytest.mark.asyncio
async def test_get_nonexistent_run(client: AsyncClient):
    """GET /runs/{id} for a nonexistent run returns 404."""
    response = await client.get("/runs/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_get_checkpoints_empty(client: AsyncClient):
    """GET /runs/{id}/checkpoints returns empty list for a new run."""
    response = await client.post(
        "/runs",
        json={
            "agent_name": "checkpoint-test",
            "prompt": "Test checkpoints endpoint.",
        },
    )
    assert response.status_code == 201
    run_id = response.json()["id"]

    resp = await client.get(f"/runs/{run_id}/checkpoints")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_unauthorized_access():
    """Requests without valid token are rejected."""
    # Need pool setup for this too since routes use PoolDep
    pool = await create_pool(settings.database_url)
    app.state.pool = pool

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as anon:
        response = await anon.get("/runs/some-id")
        assert response.status_code == 401

        response = await anon.post(
            "/runs",
            json={"agent_name": "test", "prompt": "test"},
        )
        assert response.status_code == 401

    await pool.close()
