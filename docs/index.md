# AgentBox Documentation

**AgentBox** is a minimal open-source agent platform for running untrusted, long-lived AI agent workloads in isolated sandboxes, with durable (resumable) execution backed by Postgres and full observability via Logfire/OpenTelemetry.

---

## Why AgentBox?

Running AI agents in production is hard:

- **Long-lived**: agents take 30–60+ seconds, making them crash-prone.
- **Expensive**: a mid-run crash wastes LLM API costs and compute time.
- **Untrusted**: agents run code, make network calls, and handle credentials — they need strong isolation.

AgentBox solves these problems with **durable execution**, **sandboxing**, and **least-privilege credentials**.

## Quick Links

<div class="grid" markdown>

[:material-rocket-launch: Getting Started](getting-started.md)
:   Set up AgentBox in under 5 minutes.

[:material-cogs: Architecture](architecture.md)
:   Understand the design: control plane, launcher, sandbox, and durability.

[:material-code-tags: Development](development.md)
:   Contribute to AgentBox — setup, testing, and code style.

[:material-github: GitHub](https://github.com/shenxianpeng/agentbox)
:   Source code, issues, and pull requests.

</div>

## Key Concepts

| Concept | Description |
|---|---|
| **Durable Execution** | Every model call and tool call is checkpointed to Postgres. If a container is killed mid-run, the run resumes from the last checkpoint with **zero repeated LLM calls**. |
| **Sandboxing** | Containers with resource limits, read-only rootfs, default-deny egress via a proxy, and optional gVisor integration. |
| **Least-Privilege Credentials** | Master API keys never enter the sandbox. Each run gets scoped, time-limited credentials. |
| **Full Observability** | Every span (API → scheduler → container → model/tool call) is traced in Logfire. |
| **Cost Tracking** | Estimated USD cost per run (LLM tokens + compute time). |

## Project Structure

```
agentbox/
├── pyproject.toml              # Python project config
├── Makefile                    # Build, test, and run commands
├── docker-compose.yml          # Postgres + control-plane + launcher + proxy
├── migrations/
│   └── 001_init.sql            # Database schema
├── k8s/                        # Kubernetes manifests
├── src/agentbox/
│   ├── api/                    # FastAPI control plane
│   ├── db/                     # Database layer (asyncpg)
│   ├── launcher/               # Queue poller + sandbox backends
│   ├── runner/                 # Code that runs inside the sandbox
│   ├── mcp_server/             # MCP server for Logfire telemetry
│   ├── secrets/                # Credential scoping
│   ├── cost/                   # Cost tracking
│   └── settings.py             # Configuration via pydantic-settings
├── docker/                     # Dockerfiles and configs
└── tests/
    ├── unit/                   # Unit tests
    └── e2e/                    # Integration tests
```

## Tech Stack

| Concern | Choice |
|---|---|
| Language | Python 3.12+ |
| Agent framework | [pydantic-ai](https://github.com/pydantic/pydantic-ai) |
| API | FastAPI + uvicorn |
| Database | Postgres 16, asyncpg, plain SQL migrations |
| Container runtime | Docker (default), Kubernetes Jobs |
| Egress control | Tinyproxy on internal Docker network |
| Observability | Logfire (OpenTelemetry) |
| MCP | Python MCP SDK → Logfire API |
| Package manager | `uv` |
