"""Cost breakdown tests against a real Postgres."""

from __future__ import annotations

import uuid

import pytest

from agentbox.cost.tracker import estimate_compute_cost, get_run_cost
from agentbox.db.queries import insert_run


async def test_get_run_cost_unknown_run_is_none(pool):
    assert await get_run_cost(pool, str(uuid.uuid4())) is None


async def test_get_run_cost_without_timestamps_or_checkpoints(pool):
    run = await insert_run(pool, agent_name="cost-empty", prompt="p")

    cost = await get_run_cost(pool, str(run["id"]))

    assert cost == {
        "run_id": str(run["id"]),
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "llm_cost": 0.0,
        "compute_cost": 0.0,
        "duration_seconds": 0.0,
        "model_calls": 0,
        "total_estimated_usd": 0.0,
    }


async def test_get_run_cost_aggregates_model_calls_and_compute_time(pool):
    run = await insert_run(pool, agent_name="cost-full", prompt="p")
    run_id = str(run["id"])
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE runs
            SET started_at = now() - interval '90 seconds', finished_at = now()
            WHERE id = $1::uuid
            """,
            run_id,
        )
        for idx, kind, tokens, cost in (
            (0, "model_call", 100, 0.001),
            (1, "tool_call", None, None),
            (2, "model_call", 200, 0.002),
        ):
            await conn.execute(
                """
                INSERT INTO checkpoints
                    (run_id, step_index, kind, fingerprint, payload, token_count, cost)
                VALUES ($1::uuid, $2, $3, 'fp', '{}'::jsonb, $4, $5)
                """,
                run_id,
                idx,
                kind,
                tokens,
                cost,
            )

    cost = await get_run_cost(pool, run_id)

    assert cost["model_calls"] == 2
    assert cost["total_tokens"] == 300
    assert cost["output_tokens"] == 90  # 30% heuristic
    assert cost["input_tokens"] == 210
    assert cost["llm_cost"] == pytest.approx(0.003)
    assert cost["duration_seconds"] == pytest.approx(90, abs=1)
    assert cost["compute_cost"] == pytest.approx(estimate_compute_cost(90), rel=0.05)
    assert cost["total_estimated_usd"] == pytest.approx(cost["llm_cost"] + cost["compute_cost"])
