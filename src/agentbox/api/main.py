"""FastAPI application entrypoint.

Usage:
    uv run uvicorn agentbox.api.main:app --reload
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import logfire
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from agentbox.db.queries import create_pool
from agentbox.settings import settings

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: set up DB pool and Logfire on start, clean up on stop."""
    # ── Startup ─────────────────────────────────────────────
    # Logfire (no-op if token is unset)
    if settings.logfire_token:
        logfire.configure(
            token=settings.logfire_token,
            service_name="agentbox-control-plane",
        )
        logfire.instrument_fastapi(app)
        logger.info("Logfire enabled")
    else:
        logger.info("Logfire disabled (no token)")

    # Database pool
    app.state.pool = await create_pool(settings.database_url)
    logger.info("Connected to database")

    yield

    # ── Shutdown ────────────────────────────────────────────
    await app.state.pool.close()
    logger.info("Database pool closed")


app = FastAPI(
    title="AgentBox API",
    description="Control plane for the AgentBox agent platform",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routes
from agentbox.api.routes import router  # noqa: E402

app.include_router(router, prefix="")
