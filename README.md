<div align="center">
  <h1>AgentBox</h1>
  <p><strong>A minimal open-source agent platform</strong> — durable execution, sandboxed agents, Logfire tracing.</p>
</div>

<div align="center">
  <a href="https://github.com/shenxianpeng/agentbox/actions/workflows/ci.yml"><img src="https://github.com/shenxianpeng/agentbox/actions/workflows/ci.yml/badge.svg?event=push" alt="CI"></a>
  <a href="https://pypi.python.org/pypi/agentbox"><img src="https://img.shields.io/pypi/v/agentbox.svg" alt="PyPI"></a>
  <a href="https://github.com/shenxianpeng/agentbox"><img src="https://img.shields.io/pypi/pyversions/agentbox.svg" alt="versions"></a>
  <a href="https://github.com/shenxianpeng/agentbox/blob/main/LICENSE"><img src="https://img.shields.io/github/license/shenxianpeng/agentbox.svg" alt="license"></a>
</div>

---

**AgentBox** runs untrusted, long-lived AI agent workloads in isolated sandboxes, with **durable (resumable) execution** backed by Postgres and **full observability** via Logfire/OpenTelemetry.

---

## The Problem

Running AI agents in production is hard. They're long-lived, expensive, and crash-prone. If a pod dies mid-run, you've lost money and time. Agents also need strong isolation — they run untrusted code, make network calls, and handle credentials.

**AgentBox solves this** with:
- **Durable execution**: every model call and tool call is checkpointed to Postgres. Kill the container mid-run? No problem — the run resumes from the last checkpoint with **zero repeated LLM calls**.
- **Sandboxing**: containers with resource limits, read-only rootfs, default-deny egress via a proxy, and optional gVisor.
- **Least-privilege credentials**: master API keys never enter the sandbox. Each run gets scoped, time-limited credentials.
- **Full observability**: every span (API → scheduler → container → model/tool call) is traced in Logfire.
- **Cost tracking**: estimated USD cost per run (LLM tokens + compute time).

---

## Architecture

```
            POST /runs
  client ──────────────► control-plane (FastAPI)
                              │  INSERT run (status=queued)
                              │  mint scoped credentials for run
                              ▼
                          Postgres ◄────────────────────────┐
                              ▲   checkpoints, leases,      │
        poll queue            │   heartbeats, results,      │
  launcher/worker ────────────┘   credentials              │
        │  claims run (FOR UPDATE SKIP LOCKED)              │
        │  injects scoped credentials into sandbox          │
        ▼                                                   │
  ┌─────────────────────────────┐      egress proxy         │
  │ sandbox container           │────► (allowlist only) ──► LLM API
  │  runner.py                  │
  │  └─ Pydantic AI agent       │      writes checkpoints ──┘
  │     wrapped in DurableModel │
  └─────────────────────────────┘
        all components emit OpenTelemetry spans ──► Logfire
                                          ▲
  ┌──────────────────────┐               │
  │ MCP server           │──── queries ──┘
  │ (Logfire telemetry)  │    (not Postgres directly)
  └──────────────────────┘
```

### Key Design: Durable Execution by Replay

```
Run 1 (live):         step 0 ──► step 1 ──► step 2  [CONTAINER KILLED]
                         │          │
                     checkpoint  checkpoint
                     stored ✓    stored ✓

Run 2 (resume):      step 0 ──► step 1 ──► step 2 ──► step 3
                         │          │          │
                     replayed!  replayed!   live!
                     (no LLM     (no LLM    (new work)
                      call)       call)
```

Every side-effecting operation gets a deterministic `step_index`. Before executing step N, check Postgres for a checkpoint at `(run_id, N)`. Found it? Return the stored result without re-executing. Not found? Execute, store, continue.

---

## Quickstart

### Prerequisites

- Python 3.12
- Docker + Docker Compose
- An LLM API key (DeepSeek or Anthropic)
- `uv` package manager (`curl -LsSf https://astral.sh/uv/install.sh | sh`)

### One-command demo

```bash
# Set up environment
cp .env.example .env
# Edit .env and set DEEPSEEK_API_KEY or ANTHROPIC_API_KEY

# Start Postgres, run migrations
docker compose up -d postgres
uv run python -m agentbox.db.migrate

# Build the runner image
make build-runner

# Start the launcher (in background)
uv run python -m agentbox.launcher.worker &

# Start the API server (in another terminal)
make api

# Submit a run
curl -X POST http://localhost:8000/runs \
  -H "Authorization: Bearer dev-token" \
  -H "Content-Type: application/json" \
  -d '{
    "agent_name": "incident-investigator",
    "prompt": "Investigate the web service. Analyze logs and fetch metrics."
  }'

# Check status
curl -H "Authorization: Bearer dev-token" \
  http://localhost:8000/runs/<RUN_ID>

# Get cost breakdown
curl -H "Authorization: Bearer dev-token" \
  http://localhost:8000/runs/<RUN_ID>/cost
```

### Kill-and-resume demo

```bash
# Run the automated kill-and-resume test
# (requires Docker + LLM API key)
uv run pytest tests/e2e/test_kill_and_resume.py -v
```

This test:
1. Submits a run that takes ~30-60s
2. Waits for ≥2 checkpoints
3. Force-kills the container
4. Verifies the run requeues, resumes, and completes with **zero repeated LLM calls**

### Kubernetes (kind)

```bash
# Create a kind cluster
kind create cluster --config k8s/kind-config.yaml

# Apply manifests
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/resource-quota.yaml
kubectl apply -f k8s/network-policy.yaml
kubectl apply -f k8s/rbac.yaml

# Run the launcher with K8s backend
AGENTBOX_BACKEND=k8s uv run python -m agentbox.launcher.worker
```

---

## Project Structure

```
agentbox/
├── pyproject.toml              # Python project config
├── docker-compose.yml          # Postgres + control-plane + launcher + proxy
├── Makefile                    # build-runner, up, down, test, demo, lint
├── migrations/
│   └── 001_init.sql            # tenants, runs, checkpoints, leases, credentials
├── k8s/                        # Kubernetes manifests (Phase 2)
│   ├── kind-config.yaml        # kind cluster config
│   ├── job-template.yaml       # Job template for runner pods
│   ├── rbac.yaml               # ServiceAccount + Role + RoleBinding
│   ├── resource-quota.yaml     # Quota + LimitRange
│   ├── network-policy.yaml     # Default-deny egress
│   └── runtimeclass-gvisor.yaml
├── src/agentbox/
│   ├── api/                    # FastAPI control plane
│   │   ├── main.py             # App + lifespan + Logfire
│   │   └── routes.py           # POST/GET runs, checkpoints, cost
│   ├── db/                     # Database layer
│   │   ├── migrate.py          # Migration runner
│   │   └── queries.py          # Asyncpg queries
│   ├── launcher/               # Queue poller + sandbox backends
│   │   ├── worker.py           # Poll loop + reaper + lease management
│   │   ├── backend_docker.py   # Docker SDK backend
│   │   ├── backend_k8s.py      # K8s backend (Jobs + Secrets)
│   │   └── warm_pool.py        # Pre-initialized container pool
│   ├── runner/                 # Code that runs INSIDE the sandbox
│   │   ├── main.py             # Entrypoint: load run → execute → write result
│   │   ├── durable.py          # DurableContext: checkpoint/replay engine
│   │   ├── durable_model.py    # DurableModel: wraps any pydantic-ai Model
│   │   ├── durable_tool.py     # @durable_tool decorator
│   │   ├── agents.py           # Demo "incident investigator" agent
│   │   └── credentials.py      # Scoped credential loader
│   ├── mcp_server/
│   │   └── server.py           # MCP server querying Logfire telemetry
│   ├── secrets/
│   │   └── scoper.py           # Credential minting & scoping
│   ├── cost/
│   │   └── tracker.py          # Token + compute cost estimation
│   └── settings.py             # pydantic-settings (all env vars)
├── docker/
│   ├── Dockerfile.runner       # Runner image
│   ├── Dockerfile.controlplane # Control plane + launcher image
│   └── tinyproxy.conf          # Egress proxy allowlist
└── tests/
    ├── unit/
    │   ├── test_durable.py     # Checkpoint/replay core tests (THE key tests)
    │   ├── test_mcp_server.py  # MCP server tests
    │   └── test_placeholder.py
    └── e2e/
        ├── test_api.py         # API integration tests
        └── test_kill_and_resume.py  # THE demo test
```

---

## Design Notes

### Why replay-based durability (not Temporal)?

Temporal is excellent but heavyweight. The Postgres checkpoint layer is:
- **Simple**: a few SQL queries, no external workflow engine
- **Debuggable**: checkpoints are just rows in a `checkpoints` table
- **Testable**: inject an in-memory pool for unit tests (see `test_durable.py`)

The tradeoff: the agent code must be deterministic (or replayed steps must tolerate different fingerprints). In practice, Pydantic AI agents are deterministic given the same input, so this works.

### Credential scoping

Master API keys never enter the sandbox container. When a run is created:
1. Control plane mints a scoped credential (in MVP, the same key with metadata + TTL)
2. The launcher injects `AGENTBOX_CREDENTIALS_JSON` with only the scoped credential
3. The runner reads scoped credentials via `credentials.py`

In production: use Vault or a token service to generate truly scoped, time-limited credentials.

### Multi-tenant design

The data model includes `tenant_id` from day one:
- Each `runs`, `leases` row carries a `tenant_id`
- Queue claims are per-tenant: `WHERE tenant_id = $1 ... FOR UPDATE SKIP LOCKED`
- Scheduler does round-robin across tenants for fairness
- Each tenant has a configurable `max_concurrent` limit

### Failure modes

| Failure | Behavior |
|---|---|
| Container killed mid-run | Lease expires (30s), reaper requeues run, retry with checkpoints |
| Launcher crashes | Containers keep running; new launcher picks up on restart |
| Postgres down | API returns 503; launcher retries connections |
| LLM API timeout | Model call checkpoint records error; run fails with attempt retry |
| Non-deterministic agent | Fingerprint mismatch logged as warning; cached result still used |

### What production would need

- **Firecracker/Kata microVMs**: stronger isolation than containers
- **Vault**: for true scoped credential minting
- **Per-tenant credential vault**: isolate credential blast radius per tenant
- **Fair scheduling with preemption**: weighted queues, priority classes
- **Autoscaling**: scale launcher workers based on queue depth
- **Cloud deployment**: EKS/GKE with cluster autoscaler

---

## Running Tests

```bash
# All tests (requires Postgres on localhost:5432)
make test

# Unit tests only (no Postgres needed)
make test-unit

# E2E tests (requires Postgres)
make test-e2e

# Lint
make lint

# Kill-and-resume demo (requires Docker + LLM API key)
uv run pytest tests/e2e/test_kill_and_resume.py -v
```

---

## Tech Stack

| Concern | Choice |
|---|---|
| Language | Python 3.12, managed with `uv` |
| Agent framework | `pydantic-ai` (latest) |
| API | FastAPI + uvicorn |
| DB | Postgres 16, asyncpg, plain SQL migrations |
| Container runtime | Docker (Phase 1), Kubernetes Jobs (Phase 2) |
| Egress control | Tinyproxy on internal Docker network |
| Observability | Logfire (OpenTelemetry) |
| MCP | `mcp` Python SDK, stdio server → Logfire API |

---

## License

MIT
