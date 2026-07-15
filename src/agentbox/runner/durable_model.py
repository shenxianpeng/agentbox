"""DurableModel: a pydantic-ai Model wrapper that checkpoints every model call.

This wraps any pydantic_ai.models.Model and routes its request() through
DurableContext.step(), so every LLM call is checkpointed and can be replayed.

Design note — this module is structured so it could be extracted and contributed
back to pydantic-ai as a first-class plugin or extension. The extension points
are:
  1. DurableModel wraps any Model — no changes needed to pydantic-ai internals.
  2. The checkpoint storage backend is injected via DurableContext (currently
     Postgres, but could be any key-value store).
  3. Fingerprint strategy is configurable (default: SHA-256 of serialized messages).

Usage:
    model = DurableModel(OpenAIModel('gpt-4'), context=durable_context)
    agent = Agent(model)
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
)
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import RequestUsage as Usage

from agentbox.runner.durable import DurableContext

logger = logging.getLogger(__name__)

# Cost rates per model (USD per 1K tokens) — extend as needed
# These are used for cost estimation when not provided by the API
MODEL_COST_RATES: dict[str, dict[str, float]] = {
    "deepseek-chat": {"input": 0.00027, "output": 0.00110},
    "gpt-4o": {"input": 0.00250, "output": 0.01000},
    "gpt-4o-mini": {"input": 0.00015, "output": 0.00060},
    "claude-3-5-sonnet": {"input": 0.00300, "output": 0.01500},
    "claude-3-haiku": {"input": 0.00025, "output": 0.00125},
}


def _messages_fingerprint(messages: list[ModelMessage]) -> str:
    """Compute a deterministic fingerprint for a list of model messages.

    Used to verify that the same input produces the same checkpoint.
    """
    raw = json.dumps(
        [_serialize_message(m) for m in messages],
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _serialize_message(msg: ModelMessage) -> dict[str, Any]:
    """Serialize a ModelMessage to a JSON-compatible dict."""
    if hasattr(msg, "model_dump"):
        return msg.model_dump()
    if dataclasses.is_dataclass(msg) and not isinstance(msg, type):
        return dataclasses.asdict(msg)
    return {"kind": msg.kind, "parts": [str(p) for p in getattr(msg, "parts", [])]}


def _estimate_cost(
    model_name: str,
    input_tokens: int,
    output_tokens: int,
) -> float:
    """Estimate the cost of a model call based on token usage.

    Falls back to a configurable default if the model is not in the rate table.
    """
    rates = MODEL_COST_RATES.get(model_name, {"input": 0.00015, "output": 0.00060})
    return (input_tokens * rates["input"] + output_tokens * rates["output"]) / 1000


def _deserialize_model_response(data: dict[str, Any]) -> ModelResponse:
    """Rebuild a ModelResponse from a dict (as stored by dataclasses.asdict).

    Handles all part types (text, tool-call, thinking, etc.) and preserves
    all ModelResponse metadata (usage, timestamp, run_id, etc.).

    This is needed during replay, because checkpoints store serialized dicts
    rather than live ModelResponse objects.
    """
    from pydantic_ai.messages import (
        TextPart,
        ThinkingPart,
        ToolCallPart,
        ToolReturnPart,
        UserPromptPart,
    )

    # Map part_kind to the corresponding pydantic-ai class
    PART_KIND_MAP = {
        "text": TextPart,
        "tool-call": ToolCallPart,
        "tool-return": ToolReturnPart,
        "user-prompt": UserPromptPart,
        "thinking": ThinkingPart,
        # "retry-prompt": RetryPromptPart,
        # "system-prompt": SystemPromptPart,
    }

    parts = []
    for part_dict in data.get("parts", []):
        part_kind = part_dict.get("part_kind", "")
        cls = PART_KIND_MAP.get(part_kind)
        if cls is not None:
            # Strip fields that are not constructor args
            excluded = {"part_kind", "kind"}
            filtered = {k: v for k, v in part_dict.items() if k not in excluded}
            parts.append(cls(**filtered))
        else:
            parts.append(part_dict)

    # Preserve all metadata fields from the original ModelResponse
    # Usage needs special handling: it's a dataclass that becomes a dict via asdict
    from pydantic_ai.usage import RequestUsage

    kwargs: dict[str, Any] = {
        "parts": parts,
        "model_name": data.get("model_name", ""),
    }

    usage_data = data.get("usage")
    if isinstance(usage_data, dict):
        kwargs["usage"] = RequestUsage(**usage_data)
    elif usage_data is not None:
        kwargs["usage"] = usage_data

    for field in ("timestamp", "run_id", "conversation_id",
                  "provider_name", "provider_url", "provider_response_id",
                  "finish_reason"):
        if field in data and data[field] is not None:
            kwargs[field] = data[field]

    return ModelResponse(**kwargs)


class DurableModel(Model):
    """A pydantic-ai Model wrapper that checkpoints every model call.

    Every call to `request()` is routed through DurableContext.step(), so the
    LLM response is checkpointed to Postgres. On replay, the stored response
    is returned without calling the underlying model.

    This is designed as a drop-in replacement for any pydantic-ai Model:

        model = DurableModel(inner_model, context=durable_context)
        agent = Agent(model)

    To contribute back to pydantic-ai, this class could be extracted into a
    `pydantic_ai_durable` package with:
      - A pluggable storage backend (Postgres, Redis, file, etc.)
      - Configurable fingerprint strategies
      - Telemetry hooks for observability
    """

    def __init__(
        self,
        inner: Model,
        context: DurableContext,
        cost_tracking: bool = True,
    ) -> None:
        self._inner = inner
        self._context = context
        self._cost_tracking = cost_tracking

    # ── Model interface ────────────────────────────────────

    @property
    def model_name(self) -> str:
        return self._inner.model_name

    @property
    def system(self) -> str | None:
        return getattr(self._inner, "system", None)

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        fingerprint = _messages_fingerprint(messages)

        # Estimate input tokens before making the request (best-effort)
        # Some models (e.g. TestModel) don't support count_tokens, so we handle it gracefully
        input_tokens = 0
        try:
            input_usage = await self._inner.count_tokens(
                messages, model_settings, model_request_parameters
            )
            input_tokens = input_usage.input_tokens or 0
        except NotImplementedError:
            pass

        async def _do_request() -> ModelResponse:
            return await self._inner.request(messages, model_settings, model_request_parameters)

        # We capture output_tokens after the request by wrapping _do_request
        output_tokens = 0

        async def _do_request_with_tracking() -> ModelResponse:
            nonlocal output_tokens
            resp = await self._inner.request(messages, model_settings, model_request_parameters)
            # Estimate output tokens from response content
            text_length = 0
            for part in resp.parts:
                from pydantic_ai.messages import TextPart

                if isinstance(part, TextPart):
                    text_length += len(part.content)
                elif hasattr(part, "content"):
                    text_length += len(str(part.content))
            # Rough estimate: ~4 chars per token for English text
            output_tokens = max(1, text_length // 4)
            return resp

        # Compute estimated cost
        cost = _estimate_cost(self.model_name, input_tokens, output_tokens)
        total_tokens = input_tokens + output_tokens

        result = await self._context.step(
            kind="model_call",
            fn=_do_request_with_tracking,
            fingerprint=fingerprint,
            token_count=total_tokens,
            cost=cost,
        )

        # On replay, checkpoint stores a dataclass-asdict dict.
        # Reconstruct it into a proper ModelResponse.
        if isinstance(result, dict):
            return _deserialize_model_response(result)

        return result

    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> AsyncIterator[Any]:
        """Streaming mode — not yet checkpointed. Falls through to inner model.

        TODO: checkpoint streaming responses as complete chunks.
        For Phase 1, streaming is not checkpointed; the run will fail on resume
        if it uses streaming. This is an acceptable limitation for the MVP.
        """
        async for chunk in self._inner.request_stream(
            messages, model_settings, model_request_parameters
        ):
            yield chunk

    async def compact_messages(
        self,
        messages: list[ModelMessage],
        *,
        instructions: str | None = None,
    ) -> ModelResponse:
        """Compact messages — pass through to inner model."""
        return await self._inner.compact_messages(messages, instructions=instructions)

    async def count_tokens(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> Usage:
        return await self._inner.count_tokens(messages, model_settings, model_request_parameters)

    def customize_request_parameters(
        self,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelRequestParameters:
        return self._inner.customize_request_parameters(model_request_parameters)

    def prepare_messages(
        self,
        messages: list[ModelMessage],
    ) -> list[ModelMessage]:
        return self._inner.prepare_messages(messages)

    def prepare_request(
        self,
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> tuple[ModelSettings | None, ModelRequestParameters]:
        return self._inner.prepare_request(model_settings, model_request_parameters)
