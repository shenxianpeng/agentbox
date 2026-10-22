"""Tests for the MCP server (Logfire telemetry query).

The Logfire query API is served by an in-process ``httpx.MockTransport`` and
the stdio transport is stubbed, so nothing leaves the test process.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock

import httpx
import mcp.server.stdio
import pytest

from agentbox.mcp_server import server as mcp_server
from agentbox.mcp_server.server import (
    _get_run_telemetry,
    _get_run_timeline,
    _list_runs,
    _query_logfire,
    _query_traces,
    handle_call_tool,
    handle_list_tools,
    server,
)

RUN_ID = "abc-123"


def test_mcp_server_imports() -> None:
    """Verify MCP server can be imported without errors."""
    from agentbox.mcp_server.server import server

    assert server.name == "agentbox-telemetry"


@pytest.mark.asyncio
async def test_mcp_list_tools() -> None:
    """Verify the MCP server lists the expected tools."""
    from agentbox.mcp_server.server import handle_list_tools

    tools = await handle_list_tools()
    tool_names = [t.name for t in tools]
    assert "list_runs" in tool_names
    assert "get_run_telemetry" in tool_names
    assert "get_run_timeline" in tool_names
    assert "query_traces" in tool_names


# ── fixtures ───────────────────────────────────────────────


@pytest.fixture
def logfire_api(monkeypatch):
    """Point the server at a fake Logfire query API backed by MockTransport."""
    state: dict[str, Any] = {"response": httpx.Response(200, json={"data": []}), "requests": []}
    real_client = httpx.AsyncClient

    def _handler(request: httpx.Request) -> httpx.Response:
        state["requests"].append(request)
        response = state["response"]
        if isinstance(response, Exception):
            raise response
        return response

    def _client(**kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(_handler))
        return real_client(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _client)
    monkeypatch.setattr(mcp_server, "LOGFIRE_READ_TOKEN", "read-token")
    monkeypatch.setattr(mcp_server, "LOGFIRE_API_URL", "https://logfire.test/v1/query")
    return state


class _QueryLog(list):
    """Records the SQL each tool sends; ``rows`` queues the results to return."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[dict[str, Any]]] = []


@pytest.fixture
def captured_sql(monkeypatch) -> _QueryLog:
    """Replace ``_query_logfire`` with a recorder returning queued rows."""
    log = _QueryLog()

    async def _fake(sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        log.append(sql)
        return log.rows.pop(0) if log.rows else []

    monkeypatch.setattr(mcp_server, "_query_logfire", _fake)
    return log


def _text(result) -> str:
    assert len(result) == 1 and result[0].type == "text"
    return result[0].text


# ── _query_logfire ─────────────────────────────────────────


async def test_query_logfire_without_token_returns_empty(monkeypatch, caplog):
    monkeypatch.setattr(mcp_server, "LOGFIRE_READ_TOKEN", "")
    with caplog.at_level(logging.WARNING):
        assert await _query_logfire("SELECT 1") == []
    assert "LOGFIRE_READ_TOKEN not set" in caplog.text


async def test_query_logfire_posts_sql_with_read_token(logfire_api):
    rows = [{"run_id": RUN_ID, "status": "succeeded"}]
    logfire_api["response"] = httpx.Response(200, json={"data": rows})

    assert await _query_logfire("SELECT * FROM records", {"p": 1}) == rows

    request = logfire_api["requests"][0]
    assert request.method == "POST"
    assert str(request.url) == "https://logfire.test/v1/query"
    assert request.headers["Authorization"] == "Bearer read-token"
    assert request.headers["Content-Type"] == "application/json"
    assert request.read() == b'{"sql":"SELECT * FROM records","params":{"p":1}}'


async def test_query_logfire_defaults_params_and_handles_errors(logfire_api, caplog):
    logfire_api["response"] = httpx.Response(200, json={"data": []})
    await _query_logfire("SELECT 1")
    assert b'"params":{}' in logfire_api["requests"][0].read()

    logfire_api["response"] = httpx.Response(401, text="invalid token")
    with caplog.at_level(logging.WARNING):
        assert await _query_logfire("SELECT 1") == []
    assert "Logfire query failed: 401 invalid token" in caplog.text

    logfire_api["response"] = httpx.ConnectError("dns failure")
    with caplog.at_level(logging.WARNING):
        assert await _query_logfire("SELECT 1") == []
    assert "Failed to query Logfire API: dns failure" in caplog.text


# ── tool dispatch ──────────────────────────────────────────


async def test_handle_call_tool_dispatches_every_tool(captured_sql):
    assert "No Logfire trace data" in _text(await handle_call_tool("list_runs", None))
    assert "No Logfire trace data for run `r1`" in _text(
        await handle_call_tool("get_run_telemetry", {"run_id": "r1"})
    )
    assert "No timeline data available for run `r1`" in _text(
        await handle_call_tool("get_run_timeline", {"run_id": "r1"})
    )
    assert "No results for query: SELECT 1 LIMIT 20" in _text(
        await handle_call_tool("query_traces", {"sql": "SELECT 1"})
    )
    assert len(captured_sql) == 4

    with pytest.raises(ValueError, match="Unknown tool: nope"):
        await handle_call_tool("nope", {})


async def test_list_tools_schemas_require_run_id():
    tools = {t.name: t for t in await handle_list_tools()}
    assert tools["get_run_telemetry"].inputSchema["required"] == ["run_id"]
    assert tools["get_run_timeline"].inputSchema["required"] == ["run_id"]
    assert tools["list_runs"].inputSchema["properties"]["limit"]["default"] == 10
    assert tools["query_traces"].inputSchema["properties"]["limit"]["default"] == 20


# ── list_runs ──────────────────────────────────────────────


async def test_list_runs_builds_filtered_query(captured_sql):
    captured_sql.rows.append(
        [
            {"run_id": "r1", "status": "succeeded", "timestamp": "2026-01-01T00:00:00Z"},
            {"run_id": "r2", "status": "failed", "timestamp": "2026-01-02T00:00:00Z"},
        ]
    )

    text = _text(await _list_runs({"status": "succeeded", "limit": 500}))

    sql = captured_sql[0]
    assert "attributes ? 'run_id' AND attributes->>'run_status' = 'succeeded'" in sql
    assert "LIMIT 100" in sql  # capped at 100
    assert text.startswith("## Recent Runs from Logfire")
    assert "- **r1** — status: succeeded (2026-01-01T00:00:00Z)" in text
    assert "- **r2** — status: failed (2026-01-02T00:00:00Z)" in text


async def test_list_runs_without_filter_uses_defaults(captured_sql):
    captured_sql.rows.append([{}])

    text = _text(await _list_runs({}))

    sql = captured_sql[0]
    assert "WHERE attributes ? 'run_id'\n" in sql  # no status condition
    assert "LIMIT 10" in sql
    assert "- **unknown** — status: unknown ()" in text


# ── get_run_telemetry ──────────────────────────────────────


async def test_get_run_telemetry_requires_run_id(captured_sql):
    assert _text(await _get_run_telemetry({})) == "Error: run_id is required"
    assert captured_sql == []


async def test_get_run_telemetry_marks_replayed_spans(captured_sql):
    captured_sql.rows.append(
        [
            {
                "span_name": "durable-step",
                "duration_ms": 12,
                "attributes": {"replayed": True, "step_index": 0},
            },
            {"span_name": "agent-run", "duration_ms": 340, "attributes": {"replayed": False}},
            {},
        ]
    )

    text = _text(await _get_run_telemetry({"run_id": RUN_ID}))

    assert f"attributes->>'run_id' = '{RUN_ID}'" in captured_sql[0]
    assert text.startswith(f"## Run Telemetry: {RUN_ID}")
    assert "### Span: durable-step 🔄 REPLAYED\n- Duration: 12ms" in text
    assert "### Span: agent-run\n- Duration: 340ms" in text
    assert "### Span: span-2\n- Duration: ?ms\n- Attributes: {}" in text


# ── get_run_timeline ───────────────────────────────────────


async def test_get_run_timeline_requires_run_id(captured_sql):
    assert _text(await _get_run_timeline({})) == "Error: run_id is required"
    assert captured_sql == []


async def test_get_run_timeline_lists_steps_in_order(captured_sql):
    captured_sql.rows.append(
        [
            {
                "span_name": "durable-step",
                "duration_ms": 5,
                "attributes": {"step_index": 0, "replayed": True},
            },
            {
                "span_name": "durable-step",
                "duration_ms": 900,
                "attributes": {"step_index": 1, "replayed": False},
            },
            {},
        ]
    )

    text = _text(await _get_run_timeline({"run_id": RUN_ID}))

    assert "attributes->>'step_index' IS NOT NULL" in captured_sql[0]
    assert text.startswith(f"## Timeline: {RUN_ID}")
    assert "0. **durable-step** — 5ms [replayed]" in text
    assert "1. **durable-step** — 900ms\n" in text
    assert "?. **step** — ?ms" in text


# ── query_traces ───────────────────────────────────────────


async def test_query_traces_requires_sql(captured_sql):
    assert _text(await _query_traces({})) == "Error: SQL query is required"
    assert captured_sql == []


async def test_query_traces_appends_limit_only_when_missing(captured_sql):
    captured_sql.rows.append([{"a": 1}, {"b": 2}, {"c": 3}])
    captured_sql.rows.append([{"x": 1}])

    text = _text(await _query_traces({"sql": "SELECT * FROM records", "limit": 2}))
    assert captured_sql[0] == "SELECT * FROM records LIMIT 2"
    assert text.startswith("## Trace Results (3 rows)")
    assert text.count("\n- ") == 2  # display capped at limit

    text = _text(await _query_traces({"sql": "select 1 limit 5", "limit": 500}))
    assert captured_sql[1] == "select 1 limit 5"
    assert '- {"x": 1}' in text


# ── main ───────────────────────────────────────────────────


@pytest.mark.parametrize("token", ["", "read-token"])
async def test_main_runs_server_over_stdio(monkeypatch, caplog, token):
    monkeypatch.setattr(mcp_server, "LOGFIRE_READ_TOKEN", token)
    read_stream, write_stream = object(), object()

    @asynccontextmanager
    async def _stdio_server():
        yield read_stream, write_stream

    monkeypatch.setattr(mcp.server.stdio, "stdio_server", _stdio_server)
    run = AsyncMock()
    monkeypatch.setattr(server, "run", run)

    with caplog.at_level(logging.INFO):
        await mcp_server.main()

    run.assert_awaited_once()
    args = run.await_args.args
    assert args[0] is read_stream and args[1] is write_stream
    options = args[2]
    assert options.server_name == "agentbox-telemetry"
    assert options.server_version == "0.1.0"
    assert options.capabilities.tools is not None
    if token:
        assert "Logfire query API configured" in caplog.text
    else:
        assert "MCP tools will return empty results" in caplog.text
