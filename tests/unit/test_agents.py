"""Tests for the demo incident-investigator agent and its tools.

No LLM is called: the agent is driven by pydantic-ai's ``TestModel``. The
slow tools have their ``asyncio.sleep`` patched out, and ``fetch_url`` runs
against an in-process ``httpx.MockTransport``.
"""

from __future__ import annotations

import asyncio
import hashlib

import httpx
import pytest
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.test import TestModel

from agentbox.runner.agents import (
    AVAILABLE_SERVICES,
    DEMO_AGENT_SYSTEM_PROMPT,
    analyze_logs,
    create_incident_investigator,
    fetch_metrics,
    fetch_url,
    open_github_issue,
)


@pytest.fixture
def fast_sleep(monkeypatch) -> list[float]:
    """Replace ``asyncio.sleep`` with a recorder so slow tools return instantly."""
    sleeps: list[float] = []

    async def _sleep(delay: float, *_args, **_kwargs) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", _sleep)
    return sleeps


@pytest.fixture
def mock_http(monkeypatch):
    """Route every ``httpx.AsyncClient`` through a MockTransport handler."""
    state: dict = {"handler": None, "requests": []}
    real_client = httpx.AsyncClient

    def _handler(request: httpx.Request) -> httpx.Response:
        state["requests"].append(request)
        return state["handler"](request)

    def _client(**kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(_handler))
        return real_client(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _client)
    return state


# ── tools ──────────────────────────────────────────────────


async def test_analyze_logs_known_service_is_slow_by_default(fast_sleep):
    report = await analyze_logs("Web")

    assert fast_sleep == [10]
    assert report.startswith("## Log Analysis: Web")
    assert "- **Critical Issues**: 2" in report
    assert "- **Warnings**: 5" in report
    assert "  - High latency on /api/checkout endpoint (p99=3.2s)" in report
    assert "  - Rate limiting triggered for 15% of requests" in report


async def test_analyze_logs_unknown_service_and_custom_duration(fast_sleep):
    report = await analyze_logs("billing", duration_seconds=0)

    assert fast_sleep == [0]
    assert "- **Critical Issues**: 1" in report
    assert "- **Warnings**: 2" in report
    assert "  - Routine logs for billing: no anomalies detected." in report


async def test_analyze_logs_covers_every_available_service(fast_sleep):
    for service in AVAILABLE_SERVICES:
        report = await analyze_logs(service, duration_seconds=0)
        assert f"## Log Analysis: {service}" in report
        assert "no anomalies detected" not in report


async def test_fetch_metrics(fast_sleep):
    snapshot = await fetch_metrics("api")

    assert fast_sleep == [2]
    assert snapshot.startswith("## Metrics: api")
    assert "- Error Rate: 2.1%" in snapshot
    assert "- P99 Latency: 2.1s" in snapshot


async def test_open_github_issue_is_deterministic(fast_sleep):
    title, description = "Pool exhausted", "Database connection pool exhausted"
    expected_id = int(hashlib.sha256(f"{title}:{description}".encode()).hexdigest()[:8], 16) % 10000

    first = await open_github_issue(title, description)
    second = await open_github_issue(title, description, severity="critical")

    assert fast_sleep == [1, 1]
    assert f"https://github.com/agentbox/demo/issues/{expected_id}" in first
    assert "- **Severity**: medium" in first
    assert "- **Severity**: critical" in second
    # Same content -> same issue id across processes (sha256, not hash())
    assert first.split("- **URL**")[1] == second.split("- **URL**")[1]


async def test_fetch_url_success_truncates_body(mock_http):
    body = "x" * 2500
    mock_http["handler"] = lambda request: httpx.Response(200, text=body)

    result = await fetch_url("https://allowed.example/path")

    assert mock_http["requests"][0].url == "https://allowed.example/path"
    assert "- **Status Code**: 200" in result
    assert "- **Content Length**: 2500 bytes" in result
    assert "```\n" + "x" * 2000 + "\n```" in result
    assert "x" * 2001 not in result


async def test_fetch_url_reports_egress_denial(mock_http):
    def _deny(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("CONNECT tunnel failed, response 403")

    mock_http["handler"] = _deny

    result = await fetch_url("https://example.com")

    assert result.startswith("## Fetch Failed: https://example.com")
    assert "ConnectError: CONNECT tunnel failed, response 403" in result
    assert "egress restrictions" in result


# ── agent factory ──────────────────────────────────────────


def _tool_calls(messages) -> list[str]:
    return [
        part.tool_name
        for msg in messages
        if isinstance(msg, ModelResponse)
        for part in msg.parts
        if isinstance(part, ToolCallPart)
    ]


async def test_create_incident_investigator_registers_default_tools(fast_sleep, mock_http):
    mock_http["handler"] = lambda request: httpx.Response(200, text="ok")
    model = TestModel(call_tools="all", custom_output_text="Investigation complete.")

    agent = create_incident_investigator(model)
    result = await agent.run("Investigate the web service.")

    assert result.output == "Investigation complete."
    assert set(_tool_calls(result.all_messages())) == {
        "analyze_logs",
        "fetch_metrics",
        "open_github_issue",
        "fetch_url",
    }
    assert result.all_messages()[0].parts[0].content == DEMO_AGENT_SYSTEM_PROMPT


async def test_create_incident_investigator_accepts_custom_tools():
    calls: list[str] = []

    async def ping(host: str) -> str:
        calls.append(host)
        return f"pong from {host}"

    model = TestModel(call_tools="all", custom_output_text="done")
    agent = create_incident_investigator(model, tools=[ping])
    result = await agent.run("ping")

    assert result.output == "done"
    assert _tool_calls(result.all_messages()) == ["ping"]
    assert calls == ["a"]  # TestModel's generated argument
