"""Durable tool decorator: checkpoint every tool call for replay.

Usage:
    @durable_tool(context)
    async def my_tool(arg1: str, arg2: int) -> str:
        ...
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any, ParamSpec, TypeVar

from agentbox.runner.durable import DurableContext

logger = logging.getLogger(__name__)

P = ParamSpec("P")
R = TypeVar("R")


def _tool_fingerprint(
    tool_name: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> str:
    """Compute a deterministic fingerprint for a tool call."""
    raw = json.dumps(
        {"tool": tool_name, "args": args, "kwargs": kwargs},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def durable_tool(
    context: DurableContext,
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """Decorator that checkpoints tool calls through DurableContext.

    Usage:
        context = DurableContext(run_id, pool)

        @durable_tool(context)
        async def analyze_logs(service: str) -> str:
            ...
    """

    def decorator(func: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        tool_name = func.__name__

        @functools.wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            fingerprint = _tool_fingerprint(tool_name, args, kwargs)

            async def _do_call() -> R:
                return await func(*args, **kwargs)

            result = await context.step(
                kind="tool_call",
                fn=_do_call,
                fingerprint=fingerprint,
            )
            return result

        return wrapper

    return decorator
