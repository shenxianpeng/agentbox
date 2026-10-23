"""Edge-case tests for the durable execution layer (in-memory store, no DB)."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
from typing import Any

import pytest
from pydantic import BaseModel

from agentbox.runner.durable import (
    DurableContext,
    _compute_fingerprint,
    _deserialize_payload,
    _serialize_payload,
)
from tests.unit.test_durable import InMemoryPool


@pytest.fixture
def pool() -> InMemoryPool:
    return InMemoryPool()


async def _value(v: Any) -> Any:
    return v


# ── serialization helpers ──────────────────────────────────


@dataclasses.dataclass
class Finding:
    service: str
    critical: int


class Report(BaseModel):
    title: str
    score: float


def test_serialize_payload_handles_dataclasses_models_and_plain_values():
    assert json.loads(_serialize_payload(Finding("web", 2))) == {"service": "web", "critical": 2}
    assert json.loads(_serialize_payload(Report(title="t", score=1.5))) == {
        "title": "t",
        "score": 1.5,
    }
    assert _serialize_payload({"a": [1, 2]}) == '{"a": [1, 2]}'
    # A dataclass *type* (not instance) falls through to the plain path
    assert _serialize_payload(Finding) == json.dumps(Finding, default=str)
    assert _deserialize_payload('{"a": [1, 2]}') == {"a": [1, 2]}


def test_compute_fingerprint_is_deterministic_and_order_independent():
    fp1 = _compute_fingerprint(1, "x", b=2, a=1)
    fp2 = _compute_fingerprint(1, "x", a=1, b=2)
    assert fp1 == fp2
    assert fp1 != _compute_fingerprint(1, "y", a=1, b=2)
    raw = json.dumps({"args": [1, "x"], "kwargs": {"a": 1, "b": 2}}, sort_keys=True, default=str)
    assert fp1 == hashlib.sha256(raw.encode()).hexdigest()


# ── context counters and properties ────────────────────────


async def test_context_properties_and_counters(pool):
    context = DurableContext("run-props", pool)
    assert context.run_id == "run-props"
    assert context.total_steps == 0
    assert await context.get_last_checkpoint_index() is None

    await context.step("tool_call", lambda: _value("a"))
    await context.step("tool_call", lambda: _value("b"))
    assert (context.live_count, context.replayed_count, context.total_steps) == (2, 0, 2)
    assert await context.get_last_checkpoint_index() == 1

    replay = DurableContext("run-props", pool)
    assert await replay.step("tool_call", lambda: _value("ignored")) == "a"
    assert (replay.live_count, replay.replayed_count, replay.total_steps) == (0, 1, 1)


async def test_step_without_fingerprint_hashes_the_payload(pool):
    context = DurableContext("run-nofp", pool)
    await context.step("tool_call", lambda: _value({"k": "v"}))

    stored = pool._store[("run-nofp", 0)]
    assert stored["payload"] == '{"k": "v"}'
    assert stored["fingerprint"] == _compute_fingerprint('{"k": "v"}')

    # Replay without a fingerprint skips the mismatch check and returns the payload
    replay = DurableContext("run-nofp", pool)
    assert await replay.step("tool_call", lambda: _value("new")) == {"k": "v"}
    assert replay.replayed_count == 1


async def test_step_records_static_usage(pool):
    context = DurableContext("run-usage", pool)
    await context.step("model_call", lambda: _value("r"), token_count=42, cost=0.5)
    assert pool._store[("run-usage", 0)]["token_count"] == 42
    assert pool._store[("run-usage", 0)]["cost"] == 0.5


async def test_usage_from_result_overrides_static_values(pool):
    context = DurableContext("run-usage-fn", pool)
    await context.step(
        "model_call",
        lambda: _value("rrrr"),
        token_count=1,
        cost=1.0,
        usage_from_result=lambda result: (len(result), 0.25),
    )
    assert pool._store[("run-usage-fn", 0)]["token_count"] == 4
    assert pool._store[("run-usage-fn", 0)]["cost"] == 0.25


async def test_usage_from_result_failure_is_logged_and_step_still_checkpointed(pool, caplog):
    context = DurableContext("run-usage-err", pool)

    def _boom(_result):
        raise RuntimeError("no usage")

    with caplog.at_level(logging.ERROR):
        result = await context.step(
            "model_call", lambda: _value("ok"), token_count=7, usage_from_result=_boom
        )

    assert result == "ok"
    assert "usage_from_result failed at step 0" in caplog.text
    stored = pool._store[("run-usage-err", 0)]
    assert stored["payload"] == '"ok"'
    assert stored["token_count"] == 7  # static value kept when the callable fails
    assert context.live_count == 1


# ── payload types coming back from the store ───────────────


class _DictPayloadConnection:
    """Connection whose stored payload is already decoded (like asyncpg JSONB codecs)."""

    def __init__(self, payload: Any) -> None:
        self._payload = payload

    async def fetchrow(self, query: str, *args: Any) -> dict | None:
        return {"fingerprint": "fp", "payload": self._payload, "token_count": None, "cost": None}

    async def execute(self, query: str, *args: Any) -> str:
        raise AssertionError("replay must not write")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass


class _DictPayloadPool:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def acquire(self) -> _DictPayloadConnection:
        return _DictPayloadConnection(self._payload)

    async def close(self) -> None:
        pass


async def test_replay_returns_decoded_payload_untouched():
    context = DurableContext("run-dict", _DictPayloadPool({"already": "decoded"}))

    async def _never():
        raise AssertionError("fn must not run on replay")

    assert await context.step("tool_call", _never, fingerprint="fp") == {"already": "decoded"}
    assert context.replayed_count == 1
