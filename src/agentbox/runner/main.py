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
from agentbox.runner.agents import create_incident_investigator
from agentbox.runner.credentials import get_llm_api_key, load_credentials
from agentbox.runner.durable import DurableContext
from agentbox.runner.durable_model import DurableModel
from agentbox.settings import settings

logger = logging.getLogger(__name__)

RUN_ID_ENV_VAR = "RUN_ID"
LEASE_HEARTBEAT_INTERVAL = 5  # seconds
LEASE_TTL = 30  # seconds — launcher reclaims lease if no heartbeat


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


async def main() -> int:
    """Run the agent and return exit code (0=success, 1=error)."""
    run_id = os.environ.get(RUN_ID_ENV_VAR)
    if not run_id:
        logger.error("%s not set — cannot start runner", RUN_ID_ENV_VAR)
        return 1

    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Configure Logfire
    if settings.logfire_token:
        logfire.configure(
            token=settings.logfire_token,
            service_name="agentbox-runner",
        )
        logfire.info("Runner starting", extra={"run_id": run_id})

    logger.info("Runner starting for run %s", run_id)

    try:
        # Connect to database
        pool = await create_pool(settings.database_url)

        # Get or create lease
        await get_lease_owner(pool, run_id)

        # Start heartbeat in background
        heartbeat_task = asyncio.create_task(heartbeat_lease(pool, run_id))

        # Fetch run details
        run_row = await get_run_row(pool, run_id)
        if run_row is None:
            logger.error("Run %s not found in database", run_id)
            return 1

        tenant_id = str(run_row["tenant_id"])
        agent_name = run_row["agent_name"]
        prompt = run_row["prompt"]

        # Update status to running
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE runs SET status = 'running', started_at = now() WHERE id = $1::uuid",
                run_id,
            )

        # Load scoped credentials
        creds = load_credentials()
        api_key = get_llm_api_key(creds, settings.model_name)

        # Build the agent with durable model
        # We use OpenAIModel (for DeepSeek-compatible API) or Anthropic
        from pydantic_ai.models.openai import OpenAIModel

        if api_key:
            # Use the scoped credential
            inner = OpenAIModel(
                settings.model_name,
                api_key=api_key,
                base_url=(
                    "https://api.deepseek.com/v1" if "deepseek" in settings.model_name else None
                ),
            )
        elif settings.anthropic_api_key:
            from pydantic_ai.models.anthropic import AnthropicModel

            inner = AnthropicModel(
                "claude-sonnet-4-20250514",
                api_key=settings.anthropic_api_key,
            )
        else:
            logger.error("No API key available for model %s", settings.model_name)
            return 1

        durable_context = DurableContext(run_id, pool)
        durable = DurableModel(inner, durable_context)
        agent = create_incident_investigator(durable)

        # Logfire span attributes
        logfire_span = logfire.span(
            "agent-run",
            run_id=run_id,
            tenant_id=tenant_id,
            agent_name=agent_name,
        )

        # Run the agent
        with logfire_span:
            logger.info("Starting agent execution: %s", agent_name)
            result = await agent.run(prompt)
            output = str(result.output)

        logger.info("Agent execution completed: %s", agent_name)

        # Calculate cost estimate
        total_cost = 0.0
        async with pool.acquire() as conn:
            cost_row = await conn.fetchrow(
                "SELECT SUM(cost) as total_cost FROM checkpoints WHERE run_id = $1::uuid",
                run_id,
            )
            if cost_row and cost_row["total_cost"]:
                total_cost = float(cost_row["total_cost"])

        # Write success result
        await update_run_result(
            pool,
            run_id,
            "succeeded",
            result=output,
            cost_estimate=total_cost,
        )

        logger.info("Run %s completed successfully (cost: $%.6f)", run_id, total_cost)

        # Stop heartbeat
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass

        await pool.close()
        return 0

    except Exception as exc:
        logger.exception("Runner failed for run %s", run_id)
        error_msg = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"

        try:
            pool = await create_pool(settings.database_url)
            await update_run_result(pool, run_id, "failed", error=error_msg)
            await pool.close()
        except Exception:
            logger.exception("Failed to write error result for run %s", run_id)

        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
