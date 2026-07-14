"""Unit tests for the durable execution layer.

These tests use a fake model to verify that:
  1. A fresh run executes all steps live.
  2. A replayed run fast-forwards through completed steps.
  3. The model is never re-called during replay.
  4. The final answer is identical between fresh and replayed runs.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
)
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import RequestUsage as Usage

from agentbox.runner.durable import DurableContext
from agentbox.runner.durable_model import DurableModel
from agentbox.runner.durable_tool import durable_tool

# ── Fake Model for Testing ─────────────────────────────────


class FakeModel(Model):
    """A fake model that returns predetermined responses.

    Tracks how many times it was called for verification.
    """

    def __init__(self, call_count: list[int] | None = None) -> None:
        self._call_count = call_count or [0]
        self._responses: list[ModelResponse] = []
        self._index = 0

    def add_response(self, response: ModelResponse) -> None:
        self._responses.append(response)

    @property
    def model_name(self) -> str:
        return "fake-model"

    @property
    def system(self) -> str | None:
        return None

    @property
    def call_count(self) -> int:
        return self._call_count[0]

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        self._call_count[0] += 1
        if self._index < len(self._responses):
            resp = self._responses[self._index]
            self._index += 1
            return resp
        return ModelResponse(
            parts=[TextPart(content=f"Fake response #{self._index}")],
            model_name="fake-model",
        )

    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> AsyncIterator[Any]:
        yield None

    async def compact_messages(
        self,
        messages: list[ModelMessage],
        *,
        instructions: str | None = None,
    ) -> ModelResponse:
        return await self.request(messages, None, ModelRequestParameters())

    def count_tokens(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> Usage:
        return Usage()

    def customize_request_parameters(
        self,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelRequestParameters:
        return model_request_parameters

    def prepare_messages(self, messages: list[ModelMessage]) -> list[ModelMessage]:
        return messages

    def prepare_request(
        self,
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> tuple[ModelSettings | None, ModelRequestParameters]:
        return model_settings, model_request_parameters


# ── In-memory pool for testing ─────────────────────────────


class _InMemoryConnection:
    """A minimal connection that implements fetchrow and execute against a dict."""

    def __init__(self, store: dict[tuple[str, int], dict[str, Any]]) -> None:
        self._store = store

    async def fetchrow(self, query: str, *args: Any, **kwargs: Any) -> dict | None:
        """Simulate SELECT from checkpoints table."""
        if "step_index" in query:
            run_id = str(args[0])
            step_idx = int(args[1])
            key = (run_id, step_idx)
            if key in self._store:
                entry = self._store[key]
                return {
                    "fingerprint": entry.get("fingerprint", ""),
                    "payload": entry.get("payload", "{}"),
                    "token_count": entry.get("token_count"),
                    "cost": entry.get("cost"),
                }
        elif "MAX(step_index)" in query:
            run_id = str(args[0])
            matching = [(k, v) for k, v in self._store.items() if k[0] == run_id]
            if matching:
                max_idx = max(k[1] for k, _ in matching)
                return {"max_idx": max_idx}
        return None

    async def execute(self, query: str, *args: Any) -> str:
        """Simulate INSERT into checkpoints table."""
        if "INSERT INTO checkpoints" in query:
            run_id = str(args[0])
            step_idx = int(args[1])
            key = (run_id, step_idx)
            self._store[key] = {
                "fingerprint": str(args[3]),
                "payload": args[4],
                "token_count": args[5] if len(args) > 5 else None,
                "cost": args[6] if len(args) > 6 else None,
            }
        return "INSERT 0 1"

    async def __aenter__(self) -> _InMemoryConnection:
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass


class InMemoryPool:
    """A minimal in-memory pool that mimics asyncpg.Pool's acquire().

    ``acquire()`` is a regular method (not a coroutine) that returns
    an async context manager yielding a connection-like object.
    """

    def __init__(self) -> None:
        self._store: dict[tuple[str, int], dict[str, Any]] = {}

    def acquire(self) -> _InMemoryConnection:
        """Return an async context manager for a connection."""
        return _InMemoryConnection(self._store)

    async def close(self) -> None:
        pass


@pytest.fixture
def pool():
    return InMemoryPool()


@pytest.fixture
def call_count():
    return [0]


@pytest.fixture
def fake_model(call_count):
    model = FakeModel(call_count=call_count)
    model.add_response(
        ModelResponse(
            parts=[TextPart(content="Hello! I can help you analyze.")],
            model_name="fake-model",
        )
    )
    model.add_response(
        ModelResponse(
            parts=[ToolCallPart(tool_name="analyze_logs", args={"service": "web"})],
            model_name="fake-model",
        )
    )
    model.add_response(
        ModelResponse(
            parts=[TextPart(content="Analysis complete. Found 3 issues.")],
            model_name="fake-model",
        )
    )
    return model


# ── Test Tools ─────────────────────────────────────────────


async def analyze_logs(service: str) -> str:
    """Simulate a slow tool that analyzes logs."""
    await asyncio.sleep(0.01)
    return f"Analysis of {service}: found 3 critical issues, 5 warnings."


# ── Tests ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fresh_run_records_checkpoints(pool, fake_model):
    """A fresh run should execute all steps live and record checkpoints."""
    run_id = "test-fresh-run"
    context = DurableContext(run_id, pool)
    durable = DurableModel(fake_model, context)

    agent = Agent(
        durable,
        tools=[analyze_logs],
        system_prompt="You are a helpful SRE assistant.",
    )

    result = await agent.run("Analyze the web service logs.")

    assert context.live_count > 0
    assert context.replayed_count == 0
    assert fake_model.call_count > 0
    assert result.output is not None


@pytest.mark.asyncio
async def test_replay_uses_checkpoints(pool, fake_model, call_count):
    """A replayed run should use checkpoints and not re-call the model."""
    run_id = "test-replay-run"

    # ── First run: record checkpoints ──
    context1 = DurableContext(run_id, pool)
    durable1 = DurableModel(fake_model, context1)

    agent1 = Agent(
        durable1,
        tools=[analyze_logs],
        system_prompt="You are a helpful SRE assistant.",
    )

    result1 = await agent1.run("Analyze the database performance.")

    first_call_count = call_count[0]
    first_replayed = context1.replayed_count
    first_live = context1.live_count
    first_data = result1.output

    assert first_live > 0, "First run should have live steps"
    assert first_replayed == 0, "First run should have no replayed steps"
    assert first_call_count > 0, "Model should have been called"

    # ── Second run: replay from checkpoints ──
    fake_model2 = FakeModel(call_count=[0])
    fake_model2._responses = list(fake_model._responses)
    context2 = DurableContext(run_id, pool)
    durable2 = DurableModel(fake_model2, context2)

    agent2 = Agent(
        durable2,
        tools=[analyze_logs],
        system_prompt="You are a helpful SRE assistant.",
    )

    result2 = await agent2.run("Analyze the database performance.")

    # The model should NOT have been called during replay
    assert fake_model2.call_count == 0, (
        f"Model was called {fake_model2.call_count} times during replay, expected 0"
    )

    # All steps should be replayed
    assert context2.replayed_count > 0, "Replay should have replayed steps"
    assert context2.live_count == 0, f"Replay should have 0 live steps, got {context2.live_count}"

    # The output should be the same
    assert str(result2.output) == str(first_data), (
        f"Replay output differs: {result2.output} != {first_data}"
    )


@pytest.mark.asyncio
async def test_durable_tool_checkpointing(pool):
    """The durable_tool decorator should checkpoint tool calls."""
    run_id = "test-tool-run"
    context = DurableContext(run_id, pool)

    @durable_tool(context)
    async def my_tool(query: str) -> str:
        return f"Result for: {query}"

    # Call the tool
    result = await my_tool("test query")
    assert result == "Result for: test query"
    assert context.live_count == 1
    assert context.replayed_count == 0

    # Call the same tool again with same args — this is a new step_index
    # so it should execute live (step_index goes up monotonically)
    result2 = await my_tool("test query")
    assert result2 == "Result for: test query"
    assert context.live_count == 2


@pytest.mark.asyncio
async def test_step_with_explicit_fingerprint(pool):
    """Steps with explicit fingerprint should verify on replay."""
    run_id = "test-fingerprint"
    context = DurableContext(run_id, pool)

    # Live execution
    fp = "exact-fingerprint"
    result = await context.step(
        "tool_call",
        lambda: _async_val("result-a"),
        fingerprint=fp,
    )
    assert result == "result-a"
    assert context.live_count == 1

    # Replay with matching fingerprint — starts from step_index 0
    # But wait, step_index is per-context. Let me use a new context
    # to re-read the same step_index.
    # Actually, step_index is internal to the context. The checkpoint
    # is stored at (run_id, 0). A new context with step_counter=0
    # will try step_index=0 again and find the checkpoint.
    context2 = DurableContext(run_id, pool)
    result2 = await context2.step(
        "tool_call",
        lambda: _async_val("should-not-call"),
        fingerprint=fp,
    )
    assert result2 == "result-a"
    assert context2.replayed_count == 1
    assert context2.live_count == 0

    # Replay with mismatched fingerprint
    context3 = DurableContext(run_id, pool)
    result3 = await context3.step(
        "tool_call",
        lambda: _async_val("should-not-call-either"),
        fingerprint="different-fingerprint",
    )
    assert result3 == "result-a"  # still returns original
    assert context3.replayed_count == 1


async def _async_val(v: str) -> str:
    return v
