<div align="center">
  <h1>AgentBox</h1>
  <p><strong>A minimal open-source agent platform</strong> — durable execution, sandboxed agents, Logfire tracing.</p>
</div>

<div align="center">
  <a href="https://github.com/shenxianpeng/agentbox/actions/workflows/ci.yml"><img src="https://github.com/shenxianpeng/agentbox/actions/workflows/ci.yml/badge.svg?event=push" alt="CI"></a>
  <a href="https://codecov.io/gh/shenxianpeng/agentbox"><img src="https://codecov.io/gh/shenxianpeng/agentbox/graph/badge.svg?token=PP7HFOCSN5" alt="Coverage"></a>
  <a href="https://shenxianpeng.github.io/agentbox/"><img src="https://img.shields.io/badge/docs-mkdocs-526CFE?logo=material-for-mkdocs&logoColor=white" alt="Docs"></a>
  <a href="https://github.com/shenxianpeng/agentbox/blob/main/LICENSE"><img src="https://img.shields.io/github/license/shenxianpeng/agentbox.svg" alt="license"></a>
</div>

---

**AgentBox** runs untrusted, long-lived AI agent workloads in isolated sandboxes, with **durable (resumable) execution** backed by Postgres and **full observability** via Logfire/OpenTelemetry.

> **📚 Full documentation**: [docs/](docs/index.md) — architecture guide, development guide, and getting-started tutorial.

---

## Why AgentBox?

Running AI agents in production is hard:
- They're **long-lived** (30–60s+) and crash-prone.
- A mid-run crash **wastes LLM API costs**.
- They run **untrusted code** and handle **credentials**.

**AgentBox solves this** with:
- **Durable execution**: every model call and tool call is checkpointed to Postgres. Kill the container mid-run? The run resumes from the last checkpoint with **zero repeated LLM calls**.
- **Sandboxing**: containers with resource limits, read-only rootfs, and default-deny egress via an allowlist proxy.
- **Least-privilege credentials**: master API keys never enter the sandbox or the database. A **credential proxy** stores real keys in-memory; the sandbox only gets a per-run token.
- **Row-Level Security**: the runner connects to Postgres via a restricted role (`agentbox_runner`) with RLS enforcing per-run data isolation.
- **Full observability**: every span (API → scheduler → container → model/tool call) is traced in Logfire.
- **Cost tracking**: estimated USD cost per run (LLM tokens + compute time).

---

## Quickstart

```bash
# Prerequisites: Python 3.12+, Docker, uv, LLM API key

# Install
git clone https://github.com/shenxianpeng/agentbox.git
cd agentbox
cp .env.example .env  # set your LLM API key
uv sync --all-extras --dev

# Start Postgres + run migrations
make postgres-up
make migrate

# Build the runner image
make build-runner

# Start the launcher and API (two terminals)
make dev-launcher   # Terminal 1
make dev-api        # Terminal 2

# Submit a run
curl -X POST http://localhost:8000/runs \
  -H "Authorization: Bearer dev-token" \
  -H "Content-Type: application/json" \
  -d '{"agent_name": "incident-investigator", "prompt": "Analyze the web service logs."}'
```

See the [Getting Started guide](docs/getting-started.md) for the full walkthrough.

---

See the full [documentation](docs/index.md) for details on:

| Topic | Description |
|---|---|
| [📖 Architecture](docs/architecture.md) | System design, durable execution, credential proxy, RLS |
| [🚀 Getting Started](docs/getting-started.md) | Full setup guide, Makefile reference, Kubernetes |
| [🛠️ Development](docs/development.md) | Contributing, testing, code style, module deep-dives |

---

## License

MIT
