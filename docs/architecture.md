# Architecture

AgentBox is designed around a **control-plane / launcher / sandbox** architecture with **durable execution** at its core.

---

## System Overview

```
            POST /runs
  client ──────────────► control-plane (FastAPI)
                              │  INSERT run (status=queued)
                              │  mint scoped credentials for run
                              ▼
                          Postgres ◄────────────────────────┐
                              ▲   checkpoints, leases,      │
        poll queue            │   heartbeats, results,      │
  launcher/worker ────────────┘   credentials               │
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
```

---

## Components

### 1. Control Plane (`agentbox/api/`)

A FastAPI application that:

- Accepts run submissions via `POST /runs`
- Mints scoped credentials for each run
- Returns run status, checkpoints, and cost estimates
- Exposes `GET /runs/{id}/checkpoints` and `GET /runs/{id}/cost`

The control plane is stateless — all state lives in Postgres.

### 2. Launcher Worker (`agentbox/launcher/`)

The "brain" of the system. It runs a polling loop that:

1. **Claims queued runs** using `FOR UPDATE SKIP LOCKED` (per-tenant, round-robin)
2. **Creates a lease** for each claimed run (with a 30s TTL)
3. **Starts a sandbox container** via the configured backend (Docker or Kubernetes)
4. **Injects scoped credentials** into the container
5. **Runs a reaper loop** that finds dead leases and either requeues or fails runs

This is also the **resume mechanism**: if a container is killed, the lease expires, the reaper finds it, and sets the run back to `queued`. The run will be picked up again and fast-forward through completed checkpoints.

### 3. Runner (`agentbox/runner/`)

Code that executes **inside the sandbox container**. It:

- Connects to Postgres using the scoped database URL
- Loads the run configuration
- Builds a pydantic-ai Agent wrapped in `DurableModel`
- Heartbeats the lease every 5 seconds
- Executes the agent, checkpointing every model and tool call
- Writes the final result back to Postgres

### 4. PostgreSQL Database

The single source of truth, containing:

| Table | Purpose |
|---|---|
| `tenants` | Multi-tenant configuration |
| `runs` | Run metadata, status, results |
| `checkpoints` | Step-by-step checkpoints for durable execution |
| `leases` | Container lease management (heartbeat-based) |
| `scoped_credentials` | Time-limited, scoped API credentials |

### 5. Egress Proxy (Tinyproxy)

All outbound traffic from sandbox containers goes through a Tinyproxy on an internal Docker network. This enforces **default-deny egress** — only allowlisted domains (e.g., the LLM API endpoint) are accessible.

---

## Durable Execution

This is the core innovation of AgentBox. Here's how it works:

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

### Key Design Decisions

**Why replay-based durability (not Temporal)?**

Temporal is excellent but heavyweight. The Postgres checkpoint layer is:

- **Simple**: a few SQL queries, no external workflow engine.
- **Debuggable**: checkpoints are just rows in a `checkpoints` table.
- **Testable**: inject an in-memory pool for unit tests.

The tradeoff: the agent code must be deterministic (or replayed steps must tolerate different fingerprints). In practice, pydantic-ai agents are deterministic given the same input, so this works well.

### The Step Protocol

Every side-effecting operation gets a deterministic, monotonically increasing `step_index`:

1. Before executing step N, check Postgres for a checkpoint at `(run_id, N)`
2. Found it? Return the stored result **without re-executing** (fast-forward)
3. Not found? Execute, store the result, and continue

The `DurableContext` class (in `agentbox.runner.durable`) implements this protocol.

### Fingerprint Verification

Each checkpoint stores a SHA-256 fingerprint of the input. On replay, if the fingerprint doesn't match, a warning is logged — this helps detect non-determinism in the agent code.

---

## Credential Scoping

Master API keys (**never** enter the sandbox container):

1. **Run creation**: control plane mints scoped credentials with a TTL
2. **Container launch**: launcher injects `AGENTBOX_CREDENTIALS_JSON` with only the scoped credential
3. **Inside sandbox**: runner reads scoped credentials via `credentials.py`

In production, this would use Vault or a token service for truly scoped, time-limited credentials.

---

## Failure Modes

| Failure | Behavior |
|---|---|
| Container killed mid-run | Lease expires (30s), reaper requeues run, retry with checkpoints |
| Launcher crashes | Containers keep running; new launcher picks up on restart |
| Postgres down | API returns 503; launcher retries connections |
| LLM API timeout | Model call checkpoint records error; run fails with attempt retry |
| Non-deterministic agent | Fingerprint mismatch logged as warning; cached result still used |

---

## What Production Would Need

- **Firecracker/Kata microVMs**: stronger isolation than containers
- **Vault**: for true scoped credential minting
- **Per-tenant credential vault**: isolate credential blast radius per tenant
- **Fair scheduling with preemption**: weighted queues, priority classes
- **Autoscaling**: scale launcher workers based on queue depth
- **Cloud deployment**: EKS/GKE with cluster autoscaler
