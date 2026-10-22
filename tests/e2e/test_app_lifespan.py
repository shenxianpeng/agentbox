"""FastAPI app lifespan and public health endpoints (require Postgres)."""

from __future__ import annotations

from unittest.mock import MagicMock

import asyncpg
import logfire
import pytest
from httpx import ASGITransport, AsyncClient

from agentbox.api.main import app, lifespan
from agentbox.settings import settings


async def test_lifespan_opens_and_closes_pool_without_logfire(monkeypatch):
    monkeypatch.setattr(settings, "logfire_token", "")
    configure = MagicMock()
    monkeypatch.setattr(logfire, "configure", configure)

    async with lifespan(app):
        pool = app.state.pool
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT 1") == 1

    configure.assert_not_called()
    with pytest.raises(asyncpg.InterfaceError, match="pool is closed"):
        async with pool.acquire():
            pass


async def test_lifespan_instruments_with_logfire_when_token_set(monkeypatch):
    monkeypatch.setattr(settings, "logfire_token", "lf-token")
    mocks = {
        name: MagicMock(name=name)
        for name in ("configure", "instrument_fastapi", "instrument_asyncpg", "instrument_httpx")
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(logfire, name, mock)

    async with lifespan(app):
        assert app.state.pool is not None

    mocks["configure"].assert_called_once_with(
        token="lf-token", service_name="agentbox-control-plane"
    )
    mocks["instrument_fastapi"].assert_called_once_with(app)
    mocks["instrument_asyncpg"].assert_called_once_with()
    mocks["instrument_httpx"].assert_called_once_with()


@pytest.mark.parametrize("path", ["/health", "/healthz"])
async def test_health_endpoints_need_no_auth(path):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(path)
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
