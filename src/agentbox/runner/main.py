"""Runner entrypoint — runs inside the sandbox container.

Reads RUN_ID from env, loads the run from DB, builds the agent with
DurableModel, heartbeats the lease in a background task, executes the agent,
and writes the result back to Postgres.

Usage:
    uv run python -m agentbox.runner.main
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import traceback

import asyncpg
import logfire

from agentbox.db.queries import create_pool
from agentbox.runner.agents import (
    analyze_logs,
    fetch_metrics,
    fetch_url,
    open_github_issue,
)
from agentbox.runner.credentials import get_llm_api_key, load_credentials
from agentbox.runner.durable import DurableContext
from agentbox.runner.durable_model import DurableModel
from agentbox.settings import settings

logger = logging.getLogger(__name__)

RUN_ID_ENV_VAR = "RUN_ID"
LEASE_HEARTBEAT_INTERVAL = 5  # seconds
LEASE_TTL = 30  # seconds — launcher reclaims lease if no heartbeat


# ── Helper functions ─────────────────────────────────────────────────────────


async def heartbeat_lease(pool: asyncpg.Pool, run_id: str) -> None:
    """Periodically update the lease heartbeat."""
    while True:
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE leases
                    SET heartbeat_at = now()
                    WHERE run_id = $1::uuid
                    """,
                    run_id,
                )
        except Exception:
            logger.exception("Failed to heartbeat lease for run %s", run_id)
        await asyncio.sleep(LEASE_HEARTBEAT_INTERVAL)


async def get_run_row(pool: asyncpg.Pool, run_id: str) -> dict | None:
    """Fetch the run row from the database."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, tenant_id, status, agent_name, prompt, egress_allow,
                   attempt, max_attempts, result, error, created_at
            FROM runs
            WHERE id = $1::uuid
            """,
            run_id,
        )
    return dict(row) if row else None


async def update_run_result(
    pool: asyncpg.Pool,
    run_id: str,
    status: str,
    result: str | None = None,
    error: str | None = None,
    cost_estimate: float | None = None,
) -> None:
    """Write the final result back to the database."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE runs
            SET status = $2,
                result = $3::jsonb,
                error = $4,
                cost_estimate = $5,
                finished_at = now()
            WHERE id = $1::uuid
            """,
            run_id,
            status,
            json.dumps({"output": result}) if result else None,
            error,
            cost_estimate,
        )


async def get_lease_owner(pool: asyncpg.Pool, run_id: str) -> str:
    """Get or create a lease owner identifier for this run instance."""
    import uuid

    instance_id = os.environ.get("HOSTNAME", str(uuid.uuid4()))
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO leases (run_id, owner, heartbeat_at)
            VALUES ($1::uuid, $2, now())
            ON CONFLICT (run_id) DO UPDATE
            SET owner = $2, heartbeat_at = now()
            """,
            run_id,
            instance_id,
        )
    return instance_id


def _setup_logging(run_id: str) -> None:
    """Configure logging and Logfire for the run."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if settings.logfire_token:
        logfire.configure(
            token=settings.logfire_token,
            service_name="agentbox-runner",
        )
        logfire.info("Runner starting", extra={"run_id": run_id})


def _build_model(creds: dict) -> tuple:
    """Build the LLM model based on available credentials.

    Uses the credential proxy for LLM API calls. The runner only has a
    per-run token (NOT the real API key). The credential proxy replaces
    the token with the real key before forwarding to the LLM API.

    Flow:
      1. Runner sends request to credential-proxy:9090 with Bearer <per-run-token>
      2. Credential proxy looks up the real key, replaces the header
      3. Proxy forwards to the real LLM API (DeepSeek, Anthropic, etc.)

    Returns (inner_model, model_name) tuple.
    Raises RuntimeError if no API key is available.
    """
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider

    api_key = get_llm_api_key(creds, settings.model_name)

    if api_key:
        # Use the credential proxy as the base URL.
        # The per-run token is sent as the API key; the credential proxy
        # replaces it with the real key before forwarding to the LLM API.
        credential_proxy_url = os.environ.get(
            "CREDENTIAL_PROXY_URL",
            "http://credential-proxy:9090",
        )
        provider = OpenAIProvider(
            api_key=api_key,
            base_url=credential_proxy_url,
        )
        return OpenAIChatModel(
            settings.model_name,
            provider=provider,
        ), settings.model_name

    raise RuntimeError(f"No API key available for model {settings.model_name}")


async def _calculate_total_cost(pool: asyncpg.Pool, run_id: str) -> float:
    """Sum checkpoint costs for a run."""
    async with pool.acquire() as conn:
        cost_row = await conn.fetchrow(
            "SELECT SUM(cost) as total_cost FROM checkpoints WHERE run_id = $1::uuid",
            run_id,
        )
    return float(cost_row["total_cost"]) if cost_row and cost_row["total_cost"] else 0.0


async def _set_run_status(pool: asyncpg.Pool, run_id: str, status: str) -> None:
    """Update the run's status in the database."""
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE runs SET status = $2, started_at = now() WHERE id = $1::uuid",
            run_id,
            status,
        )


async def _release_lease(pool: asyncpg.Pool, run_id: str) -> None:
    """Delete the lease for this run, so the reaper doesn't requeue it."""
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM leases WHERE run_id = $1::uuid",
            run_id,
        )


# ── Main entrypoint ──────────────────────────────────────────────────────────


async def main() -> int:
    """Run the agent and return exit code (0=success, 1=error)."""
    run_id = os.environ.get(RUN_ID_ENV_VAR)
    if not run_id:
        logger.error("%s not set — cannot start runner", RUN_ID_ENV_VAR)
        return 1

    _setup_logging(run_id)
    logger.info("Runner starting for run %s", run_id)

    # Use the restricted runner role + set app.run_id for Row-Level Security
    async def _init_conn(conn: asyncpg.Connection) -> None:
        await conn.execute("SET app.run_id = $1::text", run_id)

    pool = await asyncpg.create_pool(
        settings.runner_database_url,
        min_size=1,
        max_size=5,
        init=_init_conn,
    )

    try:
        # Claim lease and start heartbeat
        await get_lease_owner(pool, run_id)
        heartbeat_task = asyncio.create_task(heartbeat_lease(pool, run_id))

        # Load run details
        run_row = await get_run_row(pool, run_id)
        if run_row is None:
            logger.error("Run %s not found in database", run_id)
            return 1

        tenant_id = str(run_row["tenant_id"])
        agent_name = run_row["agent_name"]
        prompt = run_row["prompt"]

        # Mark as running
        await _set_run_status(pool, run_id, "running")

        # Build the durable agent
        creds = load_credentials()
        inner, _ = _build_model(creds)
        durable_context = DurableContext(run_id, pool)
        durable = DurableModel(inner, durable_context)
        # Wrap tools with durable_tool so every tool call is checkpointed
        from agentbox.runner.durable_tool import durable_tool

        # Build agent from registry using agent_name
        from agentbox.runner.agents import create_incident_investigator

        _AGENT_REGISTRY = {
            "incident-investigator": lambda model, tools: create_incident_investigator(model, tools=tools),
        }

        durable_tools = [
            durable_tool(durable_context)(analyze_logs),
            durable_tool(durable_context)(fetch_metrics),
            durable_tool(durable_context)(open_github_issue),
            durable_tool(durable_context)(fetch_url),
        ]
        agent_factory = _AGENT_REGISTRY.get(
            agent_name,
            lambda model, tools: create_incident_investigator(model, tools=tools),
        )
        agent = agent_factory(durable, durable_tools)

        # Execute the agent
        logfire_span = logfire.span(
            "agent-run",
            run_id=run_id,
            tenant_id=tenant_id,
            agent_name=agent_name,
        )
        with logfire_span:
            logger.info("Starting agent execution: %s", agent_name)
            result = await agent.run(prompt)
            output = str(result.output)

        logger.info("Agent execution completed: %s", agent_name)

        # Calculate cost and write result
        total_cost = await _calculate_total_cost(pool, run_id)
        await update_run_result(pool, run_id, "succeeded", result=output, cost_estimate=total_cost)

        logger.info("Run %s completed successfully (cost: $%.6f)", run_id, total_cost)

        # Cleanup: stop heartbeat and delete lease so reaper doesn't requeue
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
        await _release_lease(pool, run_id)

        return 0

    except Exception as exc:
        logger.exception("Runner failed for run %s", run_id)
        error_msg = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
        await update_run_result(pool, run_id, "failed", error=error_msg)
        # Delete lease so reaper doesn't try to requeue a failed run
        await _release_lease(pool, run_id)
        return 1

    finally:
        await pool.close()


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
