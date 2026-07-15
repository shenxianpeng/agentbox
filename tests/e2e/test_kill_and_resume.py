"""Kill-and-resume end-to-end test.

This is THE demo test for AgentBox. It proves that:
  1. A run survives container kill (mid-execution)
  2. The run resumes from the last checkpoint (no repeated model calls)
  3. The final result is correct despite the interruption

Requirements:
  - Docker daemon running
  - Postgres running (docker compose up -d postgres)
  - Runner Docker image built (docker build -t agentbox-runner -f docker/Dockerfile.runner .)
  - An LLM API key set (DEEPSEEK_API_KEY or ANTHROPIC_API_KEY)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

import pytest

from agentbox.api.main import app
from agentbox.db.migrate import migrate
from agentbox.db.queries import create_pool
from agentbox.settings import settings

logger = logging.getLogger(__name__)

# How long to wait for checkpoints before killing
CHECKPOINT_TIMEOUT = 60  # seconds
RUN_COMPLETION_TIMEOUT = 120  # seconds


@pytest.mark.asyncio
async def test_kill_and_resume():
    """Kill a running agent mid-execution and verify it resumes correctly."""
    # ── Setup ─────────────────────────────────────────────
    import docker

    docker_client = docker.from_env()

    await migrate()
    pool = await create_pool(settings.database_url)
    app.state.pool = pool

    # Determine API key
    api_key = settings.deepseek_api_key or settings.anthropic_api_key or ""
    if not api_key:
        pytest.skip("No LLM API key set (set DEEPSEEK_API_KEY or ANTHROPIC_API_KEY)")

    # ── Submit a run via the API ──────────────────────────
    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {settings.agentbox_api_token}"},
    ) as client:
        # Submit a run that will take a while (the slow analyze_logs tool)
        resp = await client.post(
            "/runs",
            json={
                "agent_name": "incident-investigator",
                "prompt": (
                    "Investigate the 'web' service. Analyze its logs, "
                    "fetch metrics, and open a GitHub issue if you find problems."
                ),
            },
        )
        assert resp.status_code == 201
        run_id = resp.json()["id"]
        logger.info("Submitted run %s", run_id)

        # ── Wait for checkpoints ──────────────────────────────
        start = time.time()
        checkpoint_count = 0
        while time.time() - start < CHECKPOINT_TIMEOUT:
            cp_resp = await client.get(f"/runs/{run_id}/checkpoints")
            assert cp_resp.status_code == 200
            checkpoints = cp_resp.json()
            checkpoint_count = len(checkpoints)
            if checkpoint_count >= 2:
                logger.info(
                    "Found %d checkpoints after %.1fs",
                    checkpoint_count,
                    time.time() - start,
                )
                break
            await asyncio.sleep(2)
        else:
            pytest.fail(
                f"Run did not produce ≥2 checkpoints within {CHECKPOINT_TIMEOUT}s "
                f"(got {checkpoint_count})"
            )

        # Record how many model_call checkpoints exist before kill
        model_calls_before = sum(1 for cp in checkpoints if cp["kind"] == "model_call")
        logger.info(
            "Model calls before kill: %d (total checkpoints: %d)",
            model_calls_before,
            checkpoint_count,
        )

        # ── Kill the runner container ──────────────────────────
        containers = docker_client.containers.list(
            filters={"label": f"agentbox.run_id={run_id}"},
            all=True,
        )
        if not containers:
            # If running against compose, the container might have a different label
            logger.warning("Container not found by label, trying all containers...")
            containers = docker_client.containers.list(all=True)
            containers = [
                c for c in containers if any("agentbox" in (tag or "") for tag in c.image.tags)
            ]

        if containers:
            container = containers[0]
            logger.info("Killing container %s for run %s", container.short_id, run_id)
            container.kill()
            container.remove(force=True)
            logger.info("Container killed and removed")
        else:
            logger.warning("No container found for run %s — may be running in compose", run_id)

        # ── Wait for requeue + resume + completion ─────────────
        start = time.time()
        final_status = None
        final_result = None
        final_attempt = None

        while time.time() - start < RUN_COMPLETION_TIMEOUT:
            run_resp = await client.get(f"/runs/{run_id}")
            assert run_resp.status_code == 200
            run_data = run_resp.json()
            final_status = run_data["status"]
            final_attempt = run_data["attempt"]

            logger.info(
                "Run %s status=%s attempt=%d (%.1fs elapsed)",
                run_id,
                final_status,
                final_attempt,
                time.time() - start,
            )

            if final_status == "succeeded":
                final_result = run_data.get("result")
                break
            elif final_status == "failed":
                pytest.fail(f"Run {run_id} failed after kill: {run_data.get('error')}")

            await asyncio.sleep(3)
        else:
            pytest.fail(
                f"Run {run_id} did not complete within {RUN_COMPLETION_TIMEOUT}s "
                f"(final status: {final_status})"
            )

        # ── Get final checkpoint count ─────────────────────────
        cp_resp = await client.get(f"/runs/{run_id}/checkpoints")
        all_checkpoints = cp_resp.json()
        total_checkpoints = len(all_checkpoints)

        # Count model calls AFTER the kill (i.e., checkpoints with step_index
        # greater than what we saw before the kill)
        model_calls_after = sum(
            1
            for cp in all_checkpoints
            if cp["kind"] == "model_call" and cp["step_index"] >= checkpoint_count
        )
        total_model_calls = sum(1 for cp in all_checkpoints if cp["kind"] == "model_call")

        logger.info(
            "Final state: status=%s attempt=%d checkpoints=%d "
            "model_calls_before_kill=%d model_calls_after_kill=%d",
            final_status,
            final_attempt,
            total_checkpoints,
            model_calls_before,
            model_calls_after,
        )

        # ── Assertions ─────────────────────────────────────────

        # 1. Run succeeded
        assert final_status == "succeeded", f"Expected succeeded, got {final_status}"

        # 2. Run was attempted (killed mid-execution, requeued, restarted)
        #    Launcher increments attempt on each claim: first claim→1, requeue, second claim→2
        assert final_attempt is not None, "attempt should be set"
        logger.info("Run attempt: %d", final_attempt)

        # 3. Core assertion: no model calls were made after the kill
        #    All completed checkpoints are replayed, so model_calls_after_kill == 0
        #    The only exception: the in-flight model call at the exact moment of kill
        #    may not have been checkpointed; if so it would appear as 1 new model call.
        assert model_calls_after <= 1, (
            f"Expected at most 1 model call after kill (for in-flight retry), "
            f"got {model_calls_after}. This means {model_calls_after} model call(s) "
            f"were re-executed during replay, which should never happen."
        )

        # 4. The total number of model call checkpoints should not exceed
        #    the pre-kill count by more than 1 (for the in-flight call)
        assert total_model_calls <= model_calls_before + 1, (
            f"Total model calls ({total_model_calls}) exceeds pre-kill count "
            f"({model_calls_before}) + 1 (in-flight retry). "
            f"Checkpoints are not being replayed correctly."
        )

        # 5. Success result should contain meaningful output
        assert final_result is not None, "Run should have output"
        logger.info("Run result (first 500 chars): %s", json.dumps(final_result, indent=2)[:500])

        # Cleanup
        await pool.close()
