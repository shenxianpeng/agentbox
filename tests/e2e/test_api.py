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
async def test_run_cost_endpoint(client: AsyncClient):
    """GET /runs/{id}/cost returns cost breakdown."""
    response = await client.post(
        "/runs",
        json={
            "agent_name": "cost-test",
            "prompt": "Test cost endpoint.",
        },
    )
    assert response.status_code == 201
    run_id = response.json()["id"]

    resp = await client.get(f"/runs/{run_id}/cost")
    assert resp.status_code == 200
    data = resp.json()
    assert data["run_id"] == run_id
    assert data["input_tokens"] >= 0
    assert data["output_tokens"] >= 0
    assert data["total_tokens"] >= 0
    assert data["llm_cost"] >= 0
    assert data["total_estimated_usd"] >= 0
    assert "model_calls" in data
    assert "duration_seconds" in data


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


@pytest.mark.asyncio
async def test_invalid_run_id_returns_422(client: AsyncClient):
    """A malformed run ID is rejected with 422, not a 500 from the DB layer."""
    for path in (
        "/runs/not-a-uuid",
        "/runs/not-a-uuid/checkpoints",
        "/runs/not-a-uuid/cost",
    ):
        resp = await client.get(path)
        assert resp.status_code == 422, f"{path}: expected 422, got {resp.status_code}"

    resp = await client.put("/runs/not-a-uuid/cancel")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_cancel_run(client: AsyncClient):
    """PUT /runs/{id}/cancel cancels a queued run; a second cancel returns 409."""
    response = await client.post(
        "/runs",
        json={"agent_name": "cancel-test", "prompt": "Cancel me."},
    )
    assert response.status_code == 201
    run_id = response.json()["id"]

    resp = await client.put(f"/runs/{run_id}/cancel")
    assert resp.status_code == 200
    assert resp.json()["status"] == "canceled"

    # Already canceled -> 409
    resp = await client.put(f"/runs/{run_id}/cancel")
    assert resp.status_code == 409

    # Unknown but well-formed UUID -> 404
    resp = await client.put("/runs/00000000-0000-0000-0000-00000000dead/cancel")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_invalid_token_rejected():
    """A well-formed but wrong Bearer token is rejected with 401."""
    pool = await create_pool(settings.database_url)
    app.state.pool = pool
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer definitely-wrong"},
        ) as bad:
            resp = await bad.get("/runs")
    finally:
        await pool.close()
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid token"


@pytest.mark.asyncio
async def test_create_run_with_egress_allow_and_tenant(client: AsyncClient):
    """Optional egress_allow / tenant_id fields are stored and echoed back."""
    resp = await client.post(
        "/runs",
        json={
            "agent_name": "egress-test",
            "prompt": "Fetch docs.",
            "egress_allow": ["docs.example.com"],
            "tenant_id": "00000000-0000-0000-0000-000000000001",
        },
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["egress_allow"] == ["docs.example.com"]
    assert data["tenant_id"] == "00000000-0000-0000-0000-000000000001"


@pytest.mark.asyncio
async def test_list_runs_pagination(client: AsyncClient):
    """GET /runs lists runs newest-first and honours limit/offset."""
    ids = []
    for i in range(2):
        resp = await client.post("/runs", json={"agent_name": "list-test", "prompt": f"p{i}"})
        assert resp.status_code == 201
        ids.append(resp.json()["id"])

    resp = await client.get("/runs", params={"limit": 1})
    assert resp.status_code == 200
    page = resp.json()
    assert len(page) == 1
    assert page[0]["id"] == ids[1]  # newest first
    assert page[0]["credentials"] is None  # listing never includes credential metadata

    resp = await client.get("/runs", params={"limit": 1, "offset": 1})
    assert resp.json()[0]["id"] == ids[0]


@pytest.mark.asyncio
async def test_checkpoints_and_cost_404_for_unknown_run(client: AsyncClient):
    """Sub-resources of a nonexistent run return 404, not empty data."""
    missing = "00000000-0000-0000-0000-00000000beef"
    assert (await client.get(f"/runs/{missing}/checkpoints")).status_code == 404
    assert (await client.get(f"/runs/{missing}/cost")).status_code == 404


@pytest.mark.asyncio
async def test_checkpoints_listing_omits_payload(client: AsyncClient):
    """GET /runs/{id}/checkpoints returns step metadata but never the payload."""
    resp = await client.post("/runs", json={"agent_name": "cp-list", "prompt": "p"})
    run_id = resp.json()["id"]
    async with app.state.pool.acquire() as conn:
        for idx, kind, tokens, cost in (
            (0, "model_call", 120, 0.0005),
            (1, "tool_call", None, None),
        ):
            await conn.execute(
                """
                INSERT INTO checkpoints
                    (run_id, step_index, kind, fingerprint, payload, token_count, cost)
                VALUES ($1::uuid, $2, $3, 'fp', '{"secret": "payload"}'::jsonb, $4, $5)
                """,
                run_id,
                idx,
                kind,
                tokens,
                cost,
            )

    resp = await client.get(f"/runs/{run_id}/checkpoints")

    assert resp.status_code == 200
    first, second = resp.json()
    assert first["step_index"] == 0 and first["kind"] == "model_call"
    assert first["token_count"] == 120 and first["cost"] == pytest.approx(0.0005)
    assert second["step_index"] == 1 and second["kind"] == "tool_call"
    assert second["token_count"] is None and second["cost"] is None
    assert "payload" not in first and "secret" not in resp.text
    assert first["created_at"] <= second["created_at"]
