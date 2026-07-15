"""MCP server exposing agent run telemetry from Logfire.

This MCP server provides tools for agents to query their own and other
runs' observability data via Logfire. It queries Logfire's SQL query API
(https://logfire-api.pydantic.dev/v1/query), NOT Postgres directly.

This mirrors Pydantic's vision:
  *"agents query Logfire (via MCP) for live telemetry"*

The Logfire query API accepts SQL queries over the `records` table:
  SELECT * FROM records WHERE attributes->>'run_id' = '...'

Usage:
    uv run python -m agentbox.mcp_server.server

Then in a Pydantic AI agent:
    from pydantic_ai import Agent
    from pydantic_ai.mcp import MCPServerStdio

    agent = Agent(
        'openai:gpt-4o',
        toolsets=[MCPServerStdio('uv', ['run', 'python', '-m', 'agentbox.mcp_server.server'])],
    )
    result = await agent.run("Why did run abc-123 take so long?")
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx
import mcp.server.stdio
import mcp.types as types
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions

logger = logging.getLogger(__name__)

server = Server("agentbox-telemetry")

# Logfire query API configuration
# Docs: https://logfire.pydantic.dev/docs/reference/api/query/
LOGFIRE_API_URL = os.environ.get(
    "LOGFIRE_API_URL",
    "https://logfire-api.pydantic.dev/v1/query",
)
LOGFIRE_READ_TOKEN = os.environ.get("LOGFIRE_READ_TOKEN", "")


async def _query_logfire(sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Query Logfire's SQL query API.

    Uses the Logfire read token (different from the write/ingest token).
    Queries the `records` table which contains all span/trace data.

    Args:
        sql: SQL query string (e.g. "SELECT * FROM records WHERE ...")
        params: Optional query parameters for parameterized SQL.

    Returns:
        List of result rows as dicts.
    """
    if not LOGFIRE_READ_TOKEN:
        logger.warning("LOGFIRE_READ_TOKEN not set — cannot query Logfire")
        return []

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                LOGFIRE_API_URL,
                json={
                    "sql": sql,
                    "params": params or {},
                },
                headers={
                    "Authorization": f"Bearer {LOGFIRE_READ_TOKEN}",
                    "Content-Type": "application/json",
                },
            )
            if resp.status_code == 200:
                return resp.json().get("data", resp.json())
            else:
                logger.warning(
                    "Logfire query failed: %s %s",
                    resp.status_code,
                    resp.text[:500],
                )
                return []
    except httpx.RequestError as exc:
        logger.warning("Failed to query Logfire API: %s", exc)
        return []


# ── MCP Tools ──────────────────────────────────────────────


@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    """List available telemetry query tools."""
    return [
        types.Tool(
            name="list_runs",
            description="List agent runs with optional filters. Queries Logfire records table.",
            inputSchema={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "description": "Filter by status (queued, running, succeeded, failed)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of runs to return (default 10)",
                        "default": 10,
                    },
                },
            },
        ),
        types.Tool(
            name="get_run_telemetry",
            description="Get full telemetry (spans, duration, markers) for a run from Logfire.",
            inputSchema={
                "type": "object",
                "properties": {
                    "run_id": {
                        "type": "string",
                        "description": "The run UUID to query",
                    },
                },
                "required": ["run_id"],
            },
        ),
        types.Tool(
            name="get_run_timeline",
            description="Get a step-by-step timeline of a run from Logfire checkpoint records.",
            inputSchema={
                "type": "object",
                "properties": {
                    "run_id": {
                        "type": "string",
                        "description": "The run UUID to query",
                    },
                },
                "required": ["run_id"],
            },
        ),
        types.Tool(
            name="query_traces",
            description="Run a custom SQL query against Logfire's records table.",
            inputSchema={
                "type": "object",
                "properties": {
                    "sql": {
                        "type": "string",
                        "description": "SQL query against the records table",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum results to display",
                        "default": 20,
                    },
                },
            },
        ),
    ]


@server.call_tool()
async def handle_call_tool(name: str, arguments: dict[str, Any] | None) -> list[types.TextContent]:
    """Handle MCP tool calls by querying Logfire telemetry."""
    if arguments is None:
        arguments = {}

    if name == "list_runs":
        return await _list_runs(arguments)
    elif name == "get_run_telemetry":
        return await _get_run_telemetry(arguments)
    elif name == "get_run_timeline":
        return await _get_run_timeline(arguments)
    elif name == "query_traces":
        return await _query_traces(arguments)
    else:
        raise ValueError(f"Unknown tool: {name}")


async def _list_runs(args: dict[str, Any]) -> list[types.TextContent]:
    """List runs from Logfire records."""
    status_filter = args.get("status", "")
    limit = min(args.get("limit", 10), 100)

    conditions = ["attributes ? 'run_id'"]
    if status_filter:
        conditions.append(f"attributes->>'run_status' = '{status_filter}'")

    sql = f"""
        SELECT
            attributes->>'run_id' as run_id,
            attributes->>'run_status' as status,
            attributes->>'agent_name' as agent_name,
            timestamp
        FROM records
        WHERE {" AND ".join(conditions)}
        ORDER BY timestamp DESC
        LIMIT {limit}
    """

    rows = await _query_logfire(sql)

    if not rows:
        return [
            types.TextContent(
                type="text",
                text="No Logfire trace data. Set LOGFIRE_READ_TOKEN to query telemetry.",
            )
        ]

    lines = ["## Recent Runs from Logfire\n"]
    for row in rows:
        rid = row.get("run_id", "unknown")
        st = row.get("status", "unknown")
        ts = row.get("timestamp", "")
        lines.append(f"- **{rid}** — status: {st} ({ts})")

    return [types.TextContent(type="text", text="\n".join(lines))]


async def _get_run_telemetry(args: dict[str, Any]) -> list[types.TextContent]:
    """Get full telemetry for a run from Logfire."""
    run_id = args.get("run_id", "")
    if not run_id:
        return [types.TextContent(type="text", text="Error: run_id is required")]

    sql = f"""
        SELECT
            span_name,
            attributes,
            start_timestamp,
            end_timestamp,
            duration_ms
        FROM records
        WHERE attributes->>'run_id' = '{run_id}'
        ORDER BY start_timestamp ASC
        LIMIT 100
    """

    rows = await _query_logfire(sql)

    if not rows:
        return [
            types.TextContent(
                type="text",
                text=f"No Logfire trace data for run `{run_id}`. Configure LOGFIRE_READ_TOKEN.",
            )
        ]

    lines = [f"## Run Telemetry: {run_id}\n"]
    for i, row in enumerate(rows):
        span_name = row.get("span_name", f"span-{i}")
        duration = row.get("duration_ms", "?")
        attrs = row.get("attributes", {})
        replayed = attrs.get("replayed", False)

        replay_marker = " 🔄 REPLAYED" if replayed else ""
        lines.append(f"### Span: {span_name}{replay_marker}")
        lines.append(f"- Duration: {duration}ms")
        lines.append(f"- Attributes: {json.dumps(attrs, default=str)[:200]}")
        lines.append("")

    return [types.TextContent(type="text", text="\n".join(lines))]


async def _get_run_timeline(args: dict[str, Any]) -> list[types.TextContent]:
    """Get timeline of a run from Logfire."""
    run_id = args.get("run_id", "")
    if not run_id:
        return [types.TextContent(type="text", text="Error: run_id is required")]

    sql = f"""
        SELECT
            span_name,
            attributes,
            start_timestamp,
            duration_ms
        FROM records
        WHERE attributes->>'run_id' = '{run_id}'
          AND attributes->>'step_index' IS NOT NULL
        ORDER BY (attributes->>'step_index')::int ASC
        LIMIT 200
    """

    rows = await _query_logfire(sql)

    if not rows:
        return [
            types.TextContent(
                type="text",
                text=f"No timeline data available for run `{run_id}`. ",
            )
        ]

    lines = [f"## Timeline: {run_id}\n"]
    for row in rows:
        span_name = row.get("span_name", "step")
        duration = row.get("duration_ms", "?")
        attrs = row.get("attributes", {})
        step_idx = attrs.get("step_index", "?")
        replayed = attrs.get("replayed", False)
        replay_flag = " [replayed]" if replayed else ""
        lines.append(f"{step_idx}. **{span_name}** — {duration}ms{replay_flag}")

    return [types.TextContent(type="text", text="\n".join(lines))]


async def _query_traces(args: dict[str, Any]) -> list[types.TextContent]:
    """Run a custom SQL query against Logfire."""
    sql = args.get("sql", "")
    limit = min(args.get("limit", 20), 100)

    if not sql:
        return [types.TextContent(type="text", text="Error: SQL query is required")]

    # Append limit if not present
    if "LIMIT" not in sql.upper():
        sql = f"{sql} LIMIT {limit}"

    rows = await _query_logfire(sql)

    if not rows:
        return [
            types.TextContent(
                type="text",
                text=f"No results for query: {sql[:200]}",
            )
        ]

    lines = [f"## Trace Results ({len(rows)} rows)\n"]
    for row in rows[:limit]:
        lines.append(f"- {json.dumps(row, default=str)[:300]}")

    return [types.TextContent(type="text", text="\n".join(lines))]


async def main() -> None:
    """Run the MCP server over stdio."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if LOGFIRE_READ_TOKEN:
        logger.info("Logfire query API configured at %s", LOGFIRE_API_URL)
    else:
        logger.warning("LOGFIRE_READ_TOKEN not set — MCP tools will return empty results")

    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="agentbox-telemetry",
                server_version="0.1.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
