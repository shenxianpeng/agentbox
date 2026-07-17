"""Durable execution layer: checkpoint/replay for agent runs.

The core idea:
  - Every side-effecting operation (model call, tool call) is assigned a
    deterministic, monotonically increasing step_index.
  - Before executing step N, check Postgres for a checkpoint at (run_id, N).
  - If found, return the stored result WITHOUT re-executing (fast-forward).
  - If not found, execute, store the result, and continue.

This enables kill-and-resume: if the runner is killed mid-run, on restart it
re-runs from the top but fast-forwards through completed steps, then continues
live from the first missing checkpoint.

Usage:
    context = DurableContext(run_id=run_id, pool=pool)
    result = await context.step("model_call", fingerprint, expensive_fn)
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, TypeVar

import asyncpg
import logfire


class CheckpointStore(Protocol):
    """Protocol for the pool/connection backend used by DurableContext.

    Mirrors the subset of asyncpg.Pool that we use, so tests can inject
    an in-memory replacement without needing Postgres.
    """

    def acquire(self) -> Any: ...
    async def close(self) -> None: ...


logger = logging.getLogger(__name__)

JSONable = TypeVar("JSONable")


def _compute_fingerprint(*args: Any, **kwargs: Any) -> str:
    """Compute a deterministic SHA-256 fingerprint for checkpoint verification.

    Used to detect non-determinism: if the same step_index produces a different
    fingerprint on replay, something changed (e.g. randomness in the prompt).
    """
    raw = json.dumps(
        {"args": args, "kwargs": kwargs},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _serialize_payload(obj: Any) -> str:
    """Serialize an arbitrary object to a JSON string for storage in JSONB.

    Handles dataclasses, Pydantic models, and plain dicts/lists.
    """
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return json.dumps(dataclasses.asdict(obj), default=str)
    if hasattr(obj, "model_dump"):
        return json.dumps(obj.model_dump(), default=str)
    return json.dumps(obj, default=str)


def _deserialize_payload(payload: str) -> Any:
    """Deserialize a JSON string back to a Python object."""
    return json.loads(payload)


class DurableContext:
    """Checkpoint/replay context for a single agent run.

    Thread-safe within a single async task. Each run gets its own context.

    The pool can be either an asyncpg.Pool or any object that provides
    an async ``acquire()`` method returning an async context manager
    yielding a connection-like object with ``fetchrow()`` and ``execute()``.
    """

    def __init__(self, run_id: str, pool: CheckpointStore | asyncpg.Pool) -> None:
        self._run_id = run_id
        self._pool = pool
        self._step_counter = 0
        self._replayed_count = 0
        self._live_count = 0

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def replayed_count(self) -> int:
        """Number of steps that were replayed (not re-executed)."""
        return self._replayed_count

    @property
    def live_count(self) -> int:
        """Number of steps that were executed live (not replayed)."""
        return self._live_count

    @property
    def total_steps(self) -> int:
        return self._replayed_count + self._live_count

    async def step(
        self,
        kind: str,
        fn: Callable[[], Awaitable[JSONable]],
        *,
        fingerprint: str | None = None,
        token_count: int | None = None,
        cost: float | None = None,
        usage_from_result: Callable[[Any], tuple[int | None, float | None]] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> JSONable:
        """Execute a single step with checkpoint/replay.

        Args:
            kind: One of "model_call" or "tool_call".
            fn: The async function to execute (only if no checkpoint exists).
            fingerprint: Optional deterministic hash for replay verification.
            token_count: Optional token usage for cost tracking.
            cost: Optional estimated cost in USD.
            usage_from_result: Optional callable evaluated on the live result
                to derive (token_count, cost) after execution — used for model
                calls where usage is only known once the response arrives.
                Takes precedence over the static token_count/cost arguments.
            metadata: Optional additional metadata to store with checkpoint.

        Returns:
            The result of fn(), either from cache or freshly executed.
        """
        idx = self._step_counter
        self._step_counter += 1

        # Hold a single connection for read + optional write (atomic checkpoint).
        # Tradeoff: long LLM calls (~30s) tie up a pool slot. Mitigation:
        # pool max_size=5, max 3 concurrent runs -> at most 3 connections held.
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT fingerprint, payload, token_count, cost
                FROM checkpoints
                WHERE run_id = $1::uuid AND step_index = $2
                """,
                self._run_id,
                idx,
            )

            if row is not None:
                # Replay: return stored result without calling fn
                with logfire.span(
                    "durable-step",
                    run_id=self._run_id,
                    step_index=idx,
                    kind=kind,
                    replayed=True,
                ):
                    if fingerprint is not None and row["fingerprint"] != fingerprint:
                        logfire.warn(
                            "Fingerprint mismatch at step {idx} (run {run_id}): "
                            "expected {expected}, got {got}",
                            idx=idx,
                            run_id=self._run_id,
                            expected=fingerprint,
                            got=row["fingerprint"],
                        )

                self._replayed_count += 1
                payload_data = row["payload"]
                if isinstance(payload_data, str):
                    return _deserialize_payload(payload_data)
                return payload_data

            # Live execution
            with logfire.span(
                "durable-step",
                run_id=self._run_id,
                step_index=idx,
                kind=kind,
                replayed=False,
            ):
                result = await fn()
            serialized = _serialize_payload(result)

            if usage_from_result is not None:
                try:
                    token_count, cost = usage_from_result(result)
                except Exception:
                    logger.exception("usage_from_result failed at step %d", idx)

            # Store checkpoint (same connection)
            await conn.execute(
                """
                INSERT INTO checkpoints
                    (run_id, step_index, kind, fingerprint, payload, token_count, cost)
                VALUES ($1::uuid, $2, $3, $4, $5::jsonb, $6, $7)
                ON CONFLICT (run_id, step_index) DO NOTHING
                """,
                self._run_id,
                idx,
                kind,
                fingerprint or _compute_fingerprint(serialized),
                serialized,
                token_count,
                cost,
            )

        self._live_count += 1
        return result

    async def get_last_checkpoint_index(self) -> int | None:
        """Get the highest step_index checkpointed for this run.

        Returns None if no checkpoints exist yet.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT MAX(step_index) as max_idx
                FROM checkpoints
                WHERE run_id = $1::uuid
                """,
                self._run_id,
            )
        return row["max_idx"] if row and row["max_idx"] is not None else None
