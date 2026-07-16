# Architecture

AgentBox is designed around a **control-plane / launcher / sandbox** architecture with **durable execution** at its core.

---

## System Overview

```
            POST /runs
  client ──────────────► control-plane (FastAPI)
                              │  INSERT run (status=queued)
                              │  mint per-run token (UUID) for credentials
                              ▼
                          Postgres ◄──────────────────────────┐
                              ▲   checkpoints, leases,        │
        poll queue            │   heartbeats, results          │
  launcher/worker ────────────┘                                │
        │  claims run (FOR UPDATE SKIP LOCKED)                 │
        │  registers {per_run_token → real_api_key} ─────► credential-proxy
        │  injects AGENTBOX_CREDENTIALS_JSON (token only)      │  (in-memory map)
        ▼                                                      │
  ┌─────────────────────────────┐      egress proxy            │
  │ sandbox container           │────► (tinyproxy, allowlist) ─┤──► LLM API
  │  runner.py                  │      per-run token ──────────┘
  │  └─ Pydantic AI agent       │      writes checkpoints ──────► Postgres
  │     wrapped in DurableModel │      (via agentbox_runner role + RLS)
  └─────────────────────────────┘
        all components emit OpenTelemetry spans ──► Logfire
```

---

## Components

### 1. Control Plane (`agentbox/api/`)

A FastAPI application that:

- Accepts run submissions via `POST /runs`
- Mints a **per-run token** (UUID) — the real API key **never enters the database or the sandbox**
- Returns run status, checkpoints, and cost estimates
- Exposes `GET /runs/{id}/checkpoints`, `GET /runs/{id}/cost`, and `PUT /runs/{id}/cancel`

The control plane is stateless — all state lives in Postgres.

### 2. Credential Proxy (`docker/credential_proxy.py`)

A lightweight HTTP proxy that:

- Stores an **in-memory mapping**: `{per_run_token → real_api_key, base_url, expires_at}`
- Receives LLM API requests from the sandbox (bearing the per-run token)
- Replaces the `Authorization` header with the real API key and forwards to the LLM provider
- Exposes `/admin/keys` API for the launcher to register/unregister keys
- **Never persists real API keys** to disk or database
- Checks `expires_at` on every request, rejecting expired tokens

This is the cornerstone of AgentBox's **least-privilege credential** design. The master API key lives only in:
1. The environment variable on the host running the launcher
2. The credential proxy's in-memory key store

**It never enters the sandbox container or the database.**

### 3. Launcher Worker (`agentbox/launcher/`)

The "brain" of the system. It runs two concurrent loops:

#### Poll Loop
1. **Claims queued runs** using `FOR UPDATE SKIP LOCKED` (round-robin across tenants)
2. **Creates a lease** for each claimed run (with a 30s heartbeat TTL)
3. **Registers the real API key** with the credential proxy (mapping per-run token → real key)
4. **Starts a sandbox container** via the configured backend (Docker or Kubernetes)
5. Injects `AGENTBOX_CREDENTIALS_JSON` with **only the per-run token** — no real keys

#### Reaper Loop
- Finds dead leases (no heartbeat for 30s) where status is still `running`
- Kills the corresponding container
- If max attempts exceeded: marks run as `failed` permanently
- Otherwise: sets run back to `queued` for retry

### 4. Runner (`agentbox/runner/`)

Code that executes **inside the sandbox container**. It:

- Connects to Postgres using the **restricted `agentbox_runner` role** with Row-Level Security
- Calls `SELECT set_config('app.run_id', $1, false)` to scope all RLS policies
- Loads the run configuration and credential tokens from env vars
- Builds a pydantic-ai Agent wrapped in `DurableModel`
- Heartbeats the lease every 5 seconds
- Executes the agent, checkpointing every model and tool call
- Writes the final result back to Postgres
- Deletes its lease on completion (so the reaper doesn't interfere)

### 5. PostgreSQL Database

The single source of truth, containing:

| Table | Purpose | RLS for runner |
|---|---|---|
| `tenants` | Multi-tenant configuration | **No access** (policy `USING (false)`) |
| `runs` | Run metadata, status, results | SELECT all, UPDATE only own `run_id` |
| `checkpoints` | Step-by-step checkpoints for durable execution | SELECT/INSERT only own `run_id` |
| `leases` | Container lease management (heartbeat-based) | Full CRUD only own `run_id` |
| `scoped_credentials` | Per-run tokens (UUIDs, NOT real keys) | **No access** (policy `USING (false)`) |

The runner connects using the `agentbox_runner` Postgres role, which has:
- Minimal column-level grants (only the columns the runner needs)
- Row-Level Security on every table
- `app.run_id` session variable set at connection init time, used by all RLS policies

### 6. Egress Proxy (Tinyproxy)

All outbound HTTP(S) traffic from sandbox containers goes through a Tinyproxy on an internal Docker network. This enforces **default-deny egress**:

- `FilterDefaultDeny Yes` — only explicitly allowlisted domains are reachable
- Allowlist uses **anchored POSIX basic regular expressions** (e.g., `^api\.deepseek\.com$`)
- The internal Docker network has `internal: true`, preventing direct internet access

### 7. Logfire / OpenTelemetry

All components emit spans to Logfire:
- **Control plane**: API request spans
- **Launcher**: claim and reap spans with `run_id`, `attempt`, `replayed=true/false` attributes
- **Runner**: model call and tool call spans with `run_id`, `step_index` attributes
- `asyncpg` and `httpx` are instrumented for full visibility

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

## Credential Security Model

### What happens with your API key

```
                    ┌──────────────────────┐
                    │  Launcher host        │
                    │  env: DEEPSEEK_API_KEY│
                    └──────┬───────────────┘
                           │ register key
                           ▼
                    ┌──────────────────────┐
                    │  credential-proxy     │
                    │  in-memory map:       │
                    │  token → real_key     │
                    └──────┬───────────────┘
                           │ proxy request
                           ▼
                    ┌──────────────────────┐
                    │  sandbox container    │
                    │  env: per-run token   │
                    │  (NOT the real key)   │
                    └──────────────────────┘
```

1. **At startup**: launcher reads `DEEPSEEK_API_KEY` from its environment
2. **On run claim**: launcher calls credential proxy's `/admin/keys` API:
   - Registers `{per_run_token → real_api_key, base_url, expires_at}`
   - The real key exists **only in the proxy's memory**
3. **In the sandbox**: runner only has `AGENTBOX_CREDENTIALS_JSON` with the per-run token
4. **On LLM call**: runner sends the per-run token to `credential-proxy:9090`
   - Proxy looks up the real key, replaces `Authorization` header, forwards the request
   - Proxy checks `expires_at` and rejects expired tokens

### Database RLS

The `agentbox_runner` Postgres role enforces row-level security:

- `runs`: can SELECT any row (needed for validation), but UPDATE only its own `run_id`
- `checkpoints`: SELECT/INSERT only rows matching `app.run_id`
- `leases`: full CRUD only on its own lease row
- `scoped_credentials`: **completely invisible** to the runner
- `tenants`: **completely invisible** to the runner

The runner sets `app.run_id` at connection init via `SELECT set_config('app.run_id', $1, false)`, and all RLS policies reference `current_setting('app.run_id')`.

---

## Failure Modes

| Failure | Behavior |
|---|---|
| Container killed mid-run | Lease expires (30s), reaper requeues run, retry with checkpoints |
| Launcher crashes | Containers keep running; new launcher picks up on restart |
| Postgres down | API returns 503; launcher retries connections |
| LLM API timeout | Model call checkpoint records error; run fails with attempt retry |
| Credential proxy down | Launcher logs warning, runs fail at first LLM call (no key injection) |
| Non-deterministic agent | Fingerprint mismatch logged as warning; cached result still used |
| Concurrent tool calls | Step index is sequentially assigned per checkpoint (see docs/development.md for edge cases) |

---

## Backends

### Docker

The default backend. Creates a single container per run on an internal Docker network (`agentbox-internal`) with:
- Resource limits (CPU, memory, PIDs)
- Read-only root filesystem
- No elevated capabilities
- Egress through Tinyproxy

### Kubernetes

Creates a K8s Job per run with:
- Temporary Secrets for credentials (cleaned up on completion/reap)
- Optional gVisor runtime class for stronger isolation
- NetworkPolicy with `matchExpressions` for pod-level filtering
- `automountServiceAccountToken: false` — runner pod has no K8s API access

Successfully completed Jobs are automatically cleaned up after 5 minutes (`ttlSecondsAfterFinished`).

---

## What Production Would Need

- **Firecracker/Kata microVMs**: stronger isolation than containers
- **Vault**: for true scoped credential minting with audit logging
- **Per-tenant credential vault**: isolate credential blast radius per tenant
- **Fair scheduling with preemption**: weighted queues, priority classes
- **Autoscaling**: scale launcher workers and warm pool based on queue depth
