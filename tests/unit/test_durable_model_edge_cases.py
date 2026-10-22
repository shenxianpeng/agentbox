"""Edge-case tests for DurableModel serialization, replay and cost estimation."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RequestUsage

from agentbox.runner.durable import DurableContext
from agentbox.runner.durable_model import (
    DurableModel,
    _deserialize_model_response,
    _estimate_cost,
    _messages_fingerprint,
    _serialize_message,
)
from tests.unit.test_durable import InMemoryPool


@pytest.fixture
def pool() -> InMemoryPool:
    return InMemoryPool()


# ── message serialization ──────────────────────────────────


class _PydanticMessage(BaseModel):
    kind: str = "custom"
    body: str = "hello"


def test_serialize_message_supports_models_dataclasses_and_duck_types():
    assert _serialize_message(_PydanticMessage()) == {"kind": "custom", "body": "hello"}  # type: ignore[arg-type]

    request = ModelRequest(parts=[UserPromptPart(content="hi")])
    serialized = _serialize_message(request)
    assert serialized["kind"] == "request"
    assert serialized["parts"][0]["content"] == "hi"

    duck = SimpleNamespace(kind="weird", parts=["p1", 2])
    assert _serialize_message(duck) == {"kind": "weird", "parts": ["p1", "2"]}  # type: ignore[arg-type]
    assert _serialize_message(SimpleNamespace(kind="empty")) == {"kind": "empty", "parts": []}  # type: ignore[arg-type]


def test_messages_fingerprint_changes_with_content():
    a = [ModelRequest(parts=[UserPromptPart(content="a", timestamp=None)])]  # type: ignore[arg-type]
    b = [ModelRequest(parts=[UserPromptPart(content="b", timestamp=None)])]  # type: ignore[arg-type]
    assert _messages_fingerprint(a) == _messages_fingerprint(a)
    assert _messages_fingerprint(a) != _messages_fingerprint(b)


# ── response deserialization ───────────────────────────────


def test_deserialize_model_response_rebuilds_parts_and_metadata():
    data = {
        "parts": [
            {"part_kind": "text", "content": "hello", "id": None},
            {"part_kind": "tool-call", "tool_name": "t", "args": {"x": 1}, "tool_call_id": "c1"},
            {"part_kind": "mystery", "payload": 1},
        ],
        "model_name": "fake",
        "usage": {"input_tokens": 3, "output_tokens": 4},
        "timestamp": "2026-01-01T00:00:00+00:00",
        "run_id": None,
        "provider_name": "proxy",
        "finish_reason": "stop",
    }

    resp = _deserialize_model_response(data)

    assert isinstance(resp.parts[0], TextPart) and resp.parts[0].content == "hello"
    assert isinstance(resp.parts[1], ToolCallPart) and resp.parts[1].args == {"x": 1}
    assert resp.parts[2] == {"part_kind": "mystery", "payload": 1}  # unknown kinds kept as-is
    assert resp.model_name == "fake"
    assert resp.usage == RequestUsage(input_tokens=3, output_tokens=4)
    assert resp.provider_name == "proxy"
    assert resp.finish_reason == "stop"
    assert resp.run_id is None  # None values are not forwarded


def test_deserialize_model_response_usage_variants():
    minimal = _deserialize_model_response({})
    assert minimal.parts == [] and minimal.model_name == ""
    assert minimal.usage == RequestUsage()

    usage = RequestUsage(input_tokens=9)
    passthrough = _deserialize_model_response({"usage": usage})
    assert passthrough.usage is usage


# ── cost estimation ────────────────────────────────────────


def test_estimate_cost_known_and_unknown_models():
    assert _estimate_cost("deepseek-chat", 1000, 1000) == pytest.approx(0.00027 + 0.00110)
    assert _estimate_cost("unknown", 1000, 1000) == pytest.approx(0.00015 + 0.00060)


class _Model(DurableModel):
    """Expose ``_usage_from_response`` with a fixed model name for testing."""

    @property
    def model_name(self) -> str:
        return "deepseek-chat"


def test_usage_from_response_prefers_reported_usage(pool):
    model = _Model(TestModel(), DurableContext("r", pool))

    reported = ModelResponse(
        parts=[TextPart("x" * 400)], usage=RequestUsage(input_tokens=10, output_tokens=20)
    )
    assert model._usage_from_response(reported) == (
        30,
        pytest.approx(_estimate_cost("deepseek-chat", 10, 20)),
    )

    estimated = ModelResponse(parts=[TextPart("x" * 400), TextPart("y" * 8)])
    assert estimated.usage == RequestUsage()
    assert model._usage_from_response(estimated) == (
        102,
        pytest.approx(_estimate_cost("deepseek-chat", 0, 102)),
    )

    no_usage = SimpleNamespace(usage=None, parts=[SimpleNamespace(content="abcd")])
    assert model._usage_from_response(no_usage) == (
        1,
        pytest.approx(_estimate_cost("deepseek-chat", 0, 1)),
    )

    assert model._usage_from_response(SimpleNamespace()) == (
        1,
        pytest.approx(_estimate_cost("deepseek-chat", 0, 1)),
    )


# ── cost tracking toggle ───────────────────────────────────


async def test_cost_tracking_can_be_disabled(pool):
    tracked = DurableModel(TestModel(custom_output_text="ok"), DurableContext("tracked", pool))
    await Agent(tracked).run("hi")
    assert pool._store[("tracked", 0)]["token_count"] is not None
    assert pool._store[("tracked", 0)]["cost"] is not None

    untracked = DurableModel(
        TestModel(custom_output_text="ok"), DurableContext("untracked", pool), cost_tracking=False
    )
    await Agent(untracked).run("hi")
    assert pool._store[("untracked", 0)]["token_count"] is None
    assert pool._store[("untracked", 0)]["cost"] is None


async def test_replay_rebuilds_model_response_from_checkpoint(pool):
    context = DurableContext("rebuild", pool)
    live = await Agent(DurableModel(TestModel(custom_output_text="live answer"), context)).run("q")
    assert live.output == "live answer"

    class _Never(TestModel):
        async def request(self, *args, **kwargs):
            raise AssertionError("model must not be called on replay")

    replayed = await Agent(DurableModel(_Never(), DurableContext("rebuild", pool))).run("q")
    assert replayed.output == "live answer"
    response = replayed.all_messages()[-1]
    assert isinstance(response, ModelResponse)
    assert isinstance(response.parts[0], TextPart)
