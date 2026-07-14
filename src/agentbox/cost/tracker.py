"""Cost tracking for agent runs.

Estimates the cost of each run based on:
  - Token usage from model call checkpoints
  - Compute time (wall-clock duration × configured compute cost per second)

Cost tracking is best-effort (estimate), not a billing system.
"""

from __future__ import annotations

import logging
from typing import Any

import asyncpg

from agentbox.settings import settings

logger = logging.getLogger(__name__)

# Default cost rates (USD per 1K tokens)
# These can be overridden via settings
MODEL_COST_RATES: dict[str, dict[str, float]] = {
    "deepseek-chat": {"input": 0.00027, "output": 0.00110},
    "deepseek-reasoner": {"input": 0.00055, "output": 0.00219},
    "gpt-4o": {"input": 0.00250, "output": 0.01000},
    "gpt-4o-mini": {"input": 0.00015, "output": 0.00060},
    "claude-sonnet-4-20250514": {"input": 0.00300, "output": 0.01500},
    "claude-3-5-sonnet": {"input": 0.00300, "output": 0.01500},
    "claude-3-haiku": {"input": 0.00025, "output": 0.00125},
}


def get_model_rates(model_name: str) -> dict[str, float]:
    """Get cost rates for a model, falling back to defaults."""
    return MODEL_COST_RATES.get(
        model_name,
        {
            "input": settings.cost_per_1k_input_tokens,
            "output": settings.cost_per_1k_output_tokens,
        },
    )


def estimate_llm_cost(
    model_name: str,
    input_tokens: int,
    output_tokens: int,
) -> float:
    """Estimate the cost of an LLM call.

    Returns cost in USD.
    """
    rates = get_model_rates(model_name)
    input_cost = (input_tokens * rates["input"]) / 1000
    output_cost = (output_tokens * rates["output"]) / 1000
    return round(input_cost + output_cost, 8)


def estimate_compute_cost(duration_seconds: float) -> float:
    """Estimate the compute cost for a run.

    Returns cost in USD.
    """
    return round(duration_seconds * settings.compute_cost_per_second, 8)


async def get_run_cost(pool: asyncpg.Pool, run_id: str) -> dict[str, Any] | None:
    """Get a detailed cost breakdown for a run.

    Returns None if the run doesn't exist.

    Returns a dict with:
      - input_tokens: total input tokens across all model calls
      - output_tokens: total output tokens across all model calls
      - llm_cost: estimated LLM API cost in USD
      - compute_cost: estimated compute cost in USD
      - total_estimated_usd: total estimated cost
      - model_calls: number of model call checkpoints
    """
    async with pool.acquire() as conn:
        # Check run exists
        run_row = await conn.fetchrow(
            "SELECT id, started_at, finished_at FROM runs WHERE id = $1::uuid",
            run_id,
        )
        if not run_row:
            return None

        # Aggregate token usage and cost from checkpoints
        agg = await conn.fetchrow(
            """
            SELECT
                COUNT(*) FILTER (WHERE kind = 'model_call') as model_calls,
                COALESCE(SUM(token_count) FILTER (WHERE kind = 'model_call'), 0) as total_tokens,
                COALESCE(SUM(cost) FILTER (WHERE kind = 'model_call'), 0) as llm_cost
            FROM checkpoints
            WHERE run_id = $1::uuid
            """,
            run_id,
        )

        # Get token counts by type (input vs output is approximated)
        # We split total tokens: typically ~⅓ output, ⅔ input for chat models
        total_tokens = agg["total_tokens"] or 0
        # Rough approximation: 30% output, 70% input
        output_tokens = int(total_tokens * 0.3)
        input_tokens = total_tokens - output_tokens

        llm_cost = float(agg["llm_cost"] or 0.0)

        # Calculate compute cost based on duration
        compute_cost = 0.0
        if run_row["started_at"] and run_row["finished_at"]:
            duration = (run_row["finished_at"] - run_row["started_at"]).total_seconds()
            compute_cost = estimate_compute_cost(duration)
        else:
            duration = 0.0

        return {
            "run_id": run_id,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "llm_cost": round(llm_cost, 8),
            "compute_cost": compute_cost,
            "duration_seconds": round(duration, 2),
            "model_calls": agg["model_calls"] or 0,
            "total_estimated_usd": round(llm_cost + compute_cost, 8),
        }
