"""Tests for W3C trace-context propagation across process boundaries."""

from __future__ import annotations

import logfire
import pytest

from agentbox.tracing import (
    TRACEPARENT_ENV_VAR,
    attach_traceparent,
    attach_traceparent_from_env,
    capture_traceparent,
    detach_context,
)


@pytest.fixture(scope="module", autouse=True)
def _configure_logfire():
    """Configure a local (non-sending) Logfire so spans are real SDK spans."""
    logfire.configure(send_to_logfire=False, console=False)


def test_capture_returns_none_outside_span():
    assert capture_traceparent() is None


def test_traceparent_roundtrip_preserves_trace_id():
    """A context attached from a captured traceparent joins the same trace."""
    with logfire.span("parent"):
        traceparent = capture_traceparent()

    assert traceparent is not None
    assert traceparent.startswith("00-")
    trace_id = traceparent.split("-")[1]

    # Simulate the launcher/runner process: no ambient context, attach from
    # the serialized value, and verify new spans join the original trace.
    token = attach_traceparent(traceparent)
    try:
        with logfire.span("child"):
            child_traceparent = capture_traceparent()
    finally:
        detach_context(token)

    assert child_traceparent is not None
    assert child_traceparent.split("-")[1] == trace_id


def test_attach_none_is_noop():
    assert attach_traceparent(None) is None
    detach_context(None)  # must not raise


def test_attach_from_env(monkeypatch):
    with logfire.span("api-request"):
        traceparent = capture_traceparent()
    assert traceparent is not None

    monkeypatch.setenv(TRACEPARENT_ENV_VAR, traceparent)
    token = attach_traceparent_from_env()
    try:
        with logfire.span("runner"):
            runner_traceparent = capture_traceparent()
    finally:
        detach_context(token)

    assert runner_traceparent is not None
    assert runner_traceparent.split("-")[1] == traceparent.split("-")[1]
