"""W3C trace-context propagation helpers.

AgentBox components run in separate processes (API, launcher, runner
container), so OpenTelemetry context does not flow between them on its own.
These helpers carry the context across the process boundaries:

  1. The API captures the current ``traceparent`` when a run is created and
     stores it on the run row.
  2. The launcher attaches that context before claiming/starting the run, so
     its spans join the API request's trace.
  3. The launcher injects a fresh ``traceparent`` into the runner container's
     environment (``TRACEPARENT``), and the runner attaches it at startup.

The result is a single Logfire trace: API -> launcher -> runner -> each
model/tool call (including replayed steps).
"""

from __future__ import annotations

import os

from opentelemetry import context as otel_context
from opentelemetry.propagate import extract, inject

TRACEPARENT_ENV_VAR = "TRACEPARENT"


def capture_traceparent() -> str | None:
    """Serialize the current span context as a W3C ``traceparent`` value.

    Returns None when there is no active recording span (e.g. Logfire
    disabled), so callers can treat propagation as best-effort.
    """
    carrier: dict[str, str] = {}
    inject(carrier)
    return carrier.get("traceparent")


def attach_traceparent(traceparent: str | None) -> object | None:
    """Attach a remote span context as the current context.

    Returns the attach token (pass to ``detach_context``), or None if no
    traceparent was provided.
    """
    if not traceparent:
        return None
    ctx = extract({"traceparent": traceparent})
    return otel_context.attach(ctx)


def detach_context(token: object | None) -> None:
    """Detach a context previously attached with ``attach_traceparent``."""
    if token is not None:
        otel_context.detach(token)  # type: ignore[arg-type]


def attach_traceparent_from_env() -> object | None:
    """Attach trace context from the ``TRACEPARENT`` env var (runner startup)."""
    return attach_traceparent(os.environ.get(TRACEPARENT_ENV_VAR))
