"""FastAPI routes for the control plane."""

from __future__ import annotations

import logging
from typing import Annotated, Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from agentbox.settings import settings

logger = logging.getLogger(__name__)

security_scheme = HTTPBearer(auto_error=False)


# ── Dependencies ────────────────────────────────────────────


def verify_token(
    credentials: HTTPAuthorizationCredentials | None = Security(security_scheme),
) -> None:
    """Verify the Bearer token matches AGENTBOX_API_TOKEN."""
    if not settings.agentbox_api_token:
        return  # no token configured → open access
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authorization header",
        )
    if credentials.credentials != settings.agentbox_api_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        )


async def get_pool(request: Request) -> asyncpg.Pool:
    """Retrieve the database pool from app state."""
    return request.app.state.pool


PoolDep = Annotated[asyncpg.Pool, Depends(get_pool)]

# Router with auth applied to all routes
router = APIRouter(dependencies=[Depends(verify_token)])


# ── Request / Response models ──────────────────────────────


class CreateRunRequest(BaseModel):
    agent_name: str = Field(..., min_length=1, description="Name of the agent to run")
    prompt: str = Field(..., min_length=1, description="Prompt text for the agent")
    egress_allow: list[str] | None = Field(
        default=None, description="Additional allowed egress domains"
    )
    tenant_id: str | None = Field(default=None, description="Tenant ID (default tenant if omitted)")


class RunResponse(BaseModel):
    id: str
    status: str
    tenant_id: str
    agent_name: str
    prompt: str
    egress_allow: list[str]
    attempt: int
    max_attempts: int
    result: Any = None
    error: str | None = None
    cost_estimate: float | None = None
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    credentials: list[dict[str, Any]] | None = Field(
        default=None,
        description="Scoped credentials metadata (credential values NOT included in responses)",
    )


class CheckpointResponse(BaseModel):
    step_index: int
    kind: str
    token_count: int | None = None
    cost: float | None = None
    created_at: str


class CostResponse(BaseModel):
    run_id: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    llm_cost: float
    compute_cost: float
    duration_seconds: float
    model_calls: int
    total_estimated_usd: float


# ── Routes ──────────────────────────────────────────────────


@router.get("/healthz")
async def healthz():
    """Health check endpoint for Docker/K8s probes."""
    return {"status": "ok"}


@router.get("/runs", response_model=list[RunResponse])
async def list_runs(pool: PoolDep, limit: int = 20, offset: int = 0):
    """List all runs with pagination."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, status, tenant_id, agent_name, prompt, egress_allow,
                   attempt, max_attempts, created_at, started_at, finished_at,
                   result, error, cost_estimate
            FROM runs
            ORDER BY created_at DESC
            LIMIT $1 OFFSET $2
            """,
            limit,
            offset,
        )
    return [_run_to_response(dict(r)) for r in rows]


@router.post("/runs", response_model=RunResponse, status_code=status.HTTP_201_CREATED)
async def create_run(
    body: CreateRunRequest,
    pool: PoolDep,
) -> Any:
    """Submit a new agent run."""
    from agentbox.db.queries import insert_run
    from agentbox.secrets.scoper import store_scoped_credentials

    # Determine which API key to use
    api_key = settings.deepseek_api_key or settings.anthropic_api_key or ""

    # Insert the run
    run = await insert_run(
        pool,
        agent_name=body.agent_name,
        prompt=body.prompt,
        egress_allow=body.egress_allow,
        tenant_id=body.tenant_id,
    )

    # Mint scoped credentials for this run
    # Scope is based on model_name so the runner can find the right key
    creds = await store_scoped_credentials(
        pool,
        str(run["id"]),
        api_key,
        settings.model_name,
    )

    return _run_to_response(run, creds)


@router.put("/runs/{run_id}/cancel", response_model=RunResponse)
async def cancel_run(
    run_id: str,
    pool: PoolDep,
) -> Any:
    """Cancel a run by ID.

    Only 'queued' or 'running' runs can be canceled. Runs that have already
    succeeded, failed, or been canceled are left unchanged.
    """
    from agentbox.db.queries import cancel_run as db_cancel_run

    run = await db_cancel_run(pool, run_id)
    if run is None:
        # Check if the run exists at all
        from agentbox.db.queries import get_run

        existing = await get_run(pool, run_id)
        if existing is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Run is already in status '{existing['status']}' and cannot be canceled",
        )
    return _run_to_response(run)


@router.get("/runs/{run_id}", response_model=RunResponse)
async def read_run(
    run_id: str,
    pool: PoolDep,
) -> Any:
    """Get a run by ID."""
    from agentbox.db.queries import get_run, get_scoped_credentials

    run = await get_run(pool, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")

    creds = await get_scoped_credentials(pool, run_id)
    return _run_to_response(run, creds)


@router.get("/runs/{run_id}/checkpoints", response_model=list[CheckpointResponse])
async def read_checkpoints(
    run_id: str,
    pool: PoolDep,
) -> Any:
    """Get checkpoints for a run."""
    from agentbox.db.queries import get_checkpoints, get_run

    # Verify run exists
    run = await get_run(pool, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")

    checkpoints = await get_checkpoints(pool, run_id)
    return [
        {
            "step_index": cp["step_index"],
            "kind": cp["kind"],
            "token_count": cp.get("token_count"),
            "cost": (float(cp["cost"]) if cp.get("cost") is not None else None),
            "created_at": cp["created_at"].isoformat(),
        }
        for cp in checkpoints
    ]


@router.get("/runs/{run_id}/cost", response_model=CostResponse)
async def read_run_cost(
    run_id: str,
    pool: PoolDep,
) -> Any:
    """Get cost breakdown for a run."""
    from agentbox.cost.tracker import get_run_cost

    cost = await get_run_cost(pool, run_id)
    if cost is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    return cost


# ── Helpers ─────────────────────────────────────────────────


def _serialize_dt(val: Any) -> str | None:
    """Serialize a datetime to ISO string, or return None."""
    if val is None:
        return None
    if hasattr(val, "isoformat"):
        return val.isoformat()
    return str(val)


def _run_to_response(
    run: dict[str, Any],
    creds: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Convert a run DB row to a RunResponse-compatible dict."""
    from agentbox.secrets.scoper import build_credentials_response

    resp = {
        "id": str(run["id"]),
        "status": run["status"],
        "tenant_id": str(run["tenant_id"]),
        "agent_name": run["agent_name"],
        "prompt": run["prompt"],
        "egress_allow": list(run.get("egress_allow", [])),
        "attempt": run["attempt"],
        "max_attempts": run["max_attempts"],
        "result": run.get("result"),
        "error": run.get("error"),
        "cost_estimate": (
            float(run["cost_estimate"]) if run.get("cost_estimate") is not None else None
        ),
        "created_at": _serialize_dt(run["created_at"]),
        "started_at": _serialize_dt(run.get("started_at")),
        "finished_at": _serialize_dt(run.get("finished_at")),
    }
    if creds is not None:
        resp["credentials"] = build_credentials_response(creds)
    return resp
