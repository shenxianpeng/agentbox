"""Demo agent definitions for the AgentBox runner.

This module defines the "incident investigator" agent — a Pydantic AI agent
that simulates an SRE investigating production incidents. It has 2–3 tools,
at least one intentionally slow one (to make runs 30–60s long for kill-and-resume
testing).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from pydantic_ai import Agent

logger = logging.getLogger(__name__)


# ── Tools ──────────────────────────────────────────────────


async def analyze_logs(service: str, duration_seconds: int = 10) -> str:
    """Simulate analyzing logs for a given service.

    This tool is intentionally slow (default 10s sleep) so runs take long enough
    to test kill-and-resume scenarios.

    Args:
        service: The service name to analyze (e.g. 'web', 'api', 'database').
        duration_seconds: How long the analysis takes (for simulation).

    Returns:
        A simulated analysis report.
    """
    logger.info("analyze_logs: starting analysis of %s (%ds)", service, duration_seconds)
    await asyncio.sleep(duration_seconds)

    # Simulate findings
    findings = {
        "web": {
            "critical": 2,
            "warnings": 5,
            "details": [
                "High latency on /api/checkout endpoint (p99=3.2s)",
                "Rate limiting triggered for 15% of requests",
            ],
        },
        "api": {
            "critical": 1,
            "warnings": 3,
            "details": [
                "Database connection pool exhausted (max 50 connections)",
            ],
        },
        "database": {
            "critical": 3,
            "warnings": 8,
            "details": [
                "Slow query on users table (seq scan, 2.1s avg)",
                "Replication lag of 30 seconds on replica-2",
                "Index fragmentation on orders table at 45%",
            ],
        },
    }

    svc = service.lower()
    result = findings.get(
        svc,
        {
            "critical": 1,
            "warnings": 2,
            "details": [f"Routine logs for {service}: no anomalies detected."],
        },
    )

    report = (
        f"## Log Analysis: {service}\n\n"
        f"- **Critical Issues**: {result['critical']}\n"
        f"- **Warnings**: {result['warnings']}\n"
        f"- **Details**:\n"
    )
    for detail in result["details"]:
        report += f"  - {detail}\n"

    logger.info("analyze_logs: completed analysis of %s", service)
    return report


async def fetch_metrics(service: str) -> str:
    """Fetch current metrics for a given service.

    Args:
        service: The service name to query.

    Returns:
        A simulated metrics snapshot.
    """
    logger.info("fetch_metrics: fetching metrics for %s", service)
    await asyncio.sleep(2)

    return (
        f"## Metrics: {service}\n\n"
        f"- CPU: 72.3%\n"
        f"- Memory: 4.2 GB / 8.0 GB\n"
        f"- Requests/s: 1,423\n"
        f"- Error Rate: 2.1%\n"
        f"- P50 Latency: 245ms\n"
        f"- P99 Latency: 2.1s\n"
        f"- Active Connections: 38\n"
    )


async def open_github_issue(
    title: str,
    description: str,
    severity: str = "medium",
) -> str:
    """Simulate opening a GitHub issue with findings.

    In the sandbox, this tool returns a simulated response instead of
    actually calling the GitHub API. In production it would use a scoped
    GitHub token.

    Uses a stable content-based hash (SHA-256) instead of Python's
    built-in hash() which is randomized per process and would break
    deterministic replay.

    Args:
        title: The issue title.
        description: A detailed description of the issue.
        severity: 'low', 'medium', 'high', or 'critical'.

    Returns:
        A simulated issue URL.
    """
    logger.info(
        "open_github_issue: creating issue '%s' (severity: %s)",
        title,
        severity,
    )
    await asyncio.sleep(1)

    # Use stable hash for deterministic replay across processes
    import hashlib

    stable_id = int(hashlib.sha256(f"{title}:{description}".encode()).hexdigest()[:8], 16) % 10000
    return (
        f"Issue created successfully!\n"
        f"- **Title**: {title}\n"
        f"- **Severity**: {severity}\n"
        f"- **URL**: https://github.com/agentbox/demo/issues/{stable_id}\n"
        f"- **Status**: open\n"
    )


async def fetch_url(url: str) -> str:
    """Fetch a URL and return its content.

    This tool is used to test egress control: if the egress proxy blocks
    the request, the tool will return an error message.

    Args:
        url: The URL to fetch (e.g. 'https://example.com').

    Returns:
        The response content or an error message.
    """
    logger.info("fetch_url: fetching %s", url)
    try:
        import httpx

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url, follow_redirects=True)
            content = response.text[:2000]  # Truncate to 2000 chars
            logger.info(
                "fetch_url: got response %d from %s",
                response.status_code,
                url,
            )
            return (
                f"## Fetch Result: {url}\n\n"
                f"- **Status Code**: {response.status_code}\n"
                f"- **Content Length**: {len(response.text)} bytes\n\n"
                f"```\n{content}\n```"
            )
    except Exception as exc:
        logger.warning("fetch_url: failed to fetch %s: %s", url, exc)
        return (
            f"## Fetch Failed: {url}\n\n"
            f"- **Error**: {type(exc).__name__}: {exc}\n\n"
            f"This may be due to egress restrictions. "
            f"The sandbox network only allows connections to allowlisted domains.\n"
        )


# ── Tool configurations ────────────────────────────────────

DEMO_AGENT_SYSTEM_PROMPT = """You are an **SRE Incident Investigator** AI agent.

Your goal is to analyze production issues by:
1. Analyzing relevant service logs
2. Fetching current metrics
3. Opening well-evidenced GitHub issues for any problems found

Work step by step. For each service you investigate:
- First analyze its logs
- Then fetch its metrics
- If you find issues, open a GitHub issue with the evidence

Be thorough and specific in your findings. Always include numerical data
when available.
"""

AVAILABLE_SERVICES = ["web", "api", "database"]


def create_incident_investigator(
    model: Any,
    tools: list[Any] | None = None,
) -> Agent:
    """Create the demo incident investigator agent.

    Args:
        model: A pydantic-ai Model instance (or DurableModel wrapper).
        tools: Optional list of additional tools. Defaults to the built-in tools.

    Returns:
        A configured Pydantic AI Agent.
    """
    return Agent(
        model,
        tools=tools or [analyze_logs, fetch_metrics, open_github_issue, fetch_url],
        system_prompt=DEMO_AGENT_SYSTEM_PROMPT,
    )
