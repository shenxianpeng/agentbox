"""Tests for the MCP server (Logfire telemetry query)."""

from __future__ import annotations

import pytest


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
