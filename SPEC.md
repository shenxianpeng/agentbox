# AgentBox — Implementation Plan

A minimal open-source **agent platform** that runs untrusted, long-lived AI agent
workloads in isolated sandboxes, with **durable (resumable) execution** backed by
Postgres and **full observability** via Logfire/OpenTelemetry.

We're building the infrastructure to run AI agents at scale — safely, reliably, and cheaply. Much of this is greenfield.
The goal is a platform that can run untrusted, long-lived, resource-hungry agent workloads — agentic workflows like
SRE investigators, issue-fixers, and findings agents that read a codebase, query live observability data,
and open well-evidenced GitHub issues and PRs.

> **Purpose**: portfolio project for Pydantic's *Agent Infrastructure Engineer* role.
> Every design decision below intentionally mirrors that job description.
> This document is written to be executed step-by-step by an AI coding agent
> (Claude Code or PI). Each step has explicit tasks and acceptance criteria.
> **Do not start a step until the previous step's acceptance criteria pass.**

---

## 1. Goals & Non-Goals

**Goals**

1. Submit an agent task via HTTP API; it runs inside an isolated container.
2. Every model call and tool call is checkpointed to Postgres; if the container/pod
   is killed mid-run, the run resumes from the last checkpoint (no repeated model calls).
3. Default-deny network egress for agent containers; only an explicit allowlist works.
4. **Least-privilege credential scoping**: each run receives scoped, time-limited
   credentials scoped to its declared needs — never the full platform API key.
5. End-to-end tracing in Logfire: API → scheduler → container → each model/tool call.
6. **Cost tracking**: each run estimates and records LLM token usage + compute cost.
7. Phase 2: orchestration on Kubernetes Jobs (local kind/k3d cluster), resource
   quotas, TTL cleanup, optional gVisor runtime, **fast cold-start via image caching**,
   and an MCP server that exposes **Logfire telemetry** (not raw Postgres) so agents
   can introspect their own and other runs' observability data.
8. **Multi-tenant awareness**: the data model and scheduler support tenant isolation
   from day one, even though the MVP runs single-tenant.

**Non-Goals (do NOT build these)**

- No Rust/TypeScript components. Python only.
- No Firecracker/Kata. gVisor is config-level only, and optional.
- No Temporal or external workflow engine — the Postgres checkpoint layer IS the point.
- No auth/multi-user UI. A single API token via env var is enough for MVP.
- No autoscaling, no cloud deployment. Everything runs locally via Docker + kind.

---

## 2. Architecture

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
  │     wrapped in durable layer│
  └─────────────────────────────┘
        all components emit OpenTelemetry spans ──► Logfire
                                          ▲
  ┌──────────────────────┐               │
  │ MCP server           │──── queries ──┘
  │ (Logfire telemetry)  │    (not Postgres directly)
  └──────────────────────┘
```

Key idea — **durable execution by replay**: the runner assigns a deterministic,
monotonically increasing `step_index` to every side-effecting operation (model
request, tool call). Before executing step N it checks Postgres; if a checkpoint
for (run_id, N) exists, it returns the stored result instead of re-executing.
On restart, the agent code re-runs from the top but "fast-forwards" through
completed steps, then continues live from the first missing checkpoint.

**Credential scoping**: when a run is created, the control plane mints scoped
credentials (e.g. a run-specific API key with a TTL equal to `max_attempts * expected_duration`,
or a temporary access token). The runner container only ever sees these scoped
credentials, never the master API keys. This limits the blast radius if a sandbox
is compromised.

**Multi-tenant data model**: every table carries an optional `tenant_id` column.
The scheduler can enforce per-tenant concurrency limits and resource quotas.
The MVP runs a single tenant but the design avoids a painful migration later.

---

## 3. Tech Stack (pinned choices)

| Concern | Choice |
|---|---|
| Language | Python 3.12, managed with `uv` |
| Agent framework | `pydantic-ai` (latest); durable layer contributed as a reusable wrapper |
| API | FastAPI + uvicorn |
| DB | Postgres 16, async access via `asyncpg`; plain SQL migrations (no ORM) |
| Container runtime (Phase 1) | Docker via `docker` Python SDK |
| Orchestration (Phase 2) | Kubernetes Jobs via `kubernetes` Python client, local `kind` cluster |
| Egress control | dedicated proxy container (tinyproxy or mitmproxy) + internal Docker network |
| Observability | `logfire` SDK (OpenTelemetry under the hood) |
| MCP (Phase 2) | `mcp` Python SDK, stdio server — queries **Logfire API** for telemetry |
| Lint/test | ruff, pytest, pytest-asyncio |
| LLM | DeepSeek API via `DEEPSEEK_API_KEY` (model configurable via env) |
| Secret management | Control-plane-side credential scoping; no vault in MVP, but documented extension point |

---

## 4. Repository Layout

```
agentbox/
├── pyproject.toml            # uv project, workspace-style single package
├── docker-compose.yml        # postgres + egress-proxy + control-plane + launcher
├── migrations/               # 001_init.sql, 002_...
├── src/agentbox/
│   ├── api/                  # FastAPI app (control plane)
│   │   ├── main.py
│   │   └── routes.py
│   ├── db/                   # asyncpg pool, queries, migration runner
│   ├── launcher/             # queue poller + sandbox backends
│   │   ├── worker.py
│   │   ├── backend_docker.py
│   │   └── backend_k8s.py    # Phase 2
│   ├── runner/               # code that runs INSIDE the sandbox
│   │   ├── main.py           # entrypoint: load run, build agent, execute
│   │   ├── durable.py        # checkpoint/replay layer
│   │   ├── credentials.py    # scoped credential loader (reads injected env)
│   │   └── agents.py         # demo agent definitions + tools
│   ├── mcp_server/           # Phase 2
│   │   └── server.py         # MCP server querying Logfire API
│   ├── secrets/              # credential scoping & minting
│   │   ├── scoper.py         # mint scoped creds per run
│   │   └── models.py         # credential data models
│   ├── cost/                 # Phase 2: cost tracking
│   │   └── tracker.py        # estimate token/compute cost per run
│   └── settings.py           # pydantic-settings, all env vars
├── docker/
│   ├── Dockerfile.runner
│   └── Dockerfile.controlplane
├── k8s/                      # Phase 2: kind config, RBAC, Job template, RuntimeClass
├── tests/
│   ├── unit/
│   └── e2e/
│       ├── test_kill_and_resume.py   # THE demo test
│       ├── test_credential_scoping.py # scoped creds cannot access master keys
│       └── test_cost_tracking.py     # cost recorded correctly
└── README.md
```

---

## 5. Data Model (Postgres)

`migrations/001_init.sql`:

```sql
CREATE TABLE tenants (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT NOT NULL UNIQUE,
    max_concurrent  INT  NOT NULL DEFAULT 5,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO tenants (id, name) VALUES ('00000000-0000-0000-0000-000000000001', 'default');

CREATE TABLE runs (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     UUID NOT NULL REFERENCES tenants(id) DEFAULT '00000000-0000-0000-0000-000000000001',
    status        TEXT NOT NULL DEFAULT 'queued',
        -- queued | running | succeeded | failed | canceled
    agent_name    TEXT NOT NULL,
    prompt        TEXT NOT NULL,
    egress_allow  TEXT[] NOT NULL DEFAULT '{}',   -- extra allowed domains
    attempt       INT  NOT NULL DEFAULT 0,
    max_attempts  INT  NOT NULL DEFAULT 3,
    result        JSONB,
    error         TEXT,
    cost_estimate NUMERIC(10,6),                  -- estimated USD cost (Phase 2)
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at    TIMESTAMPTZ,
    finished_at   TIMESTAMPTZ
);

CREATE INDEX runs_queue_idx ON runs (tenant_id, status, created_at);

CREATE TABLE checkpoints (
    run_id      UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    step_index  INT  NOT NULL,
    kind        TEXT NOT NULL,          -- model_call | tool_call
    fingerprint TEXT NOT NULL,          -- hash of the request, for replay sanity check
    payload     JSONB NOT NULL,         -- the recorded result
    token_count INT,                    -- token usage for cost tracking
    cost        NUMERIC(10,6),         -- estimated cost of this step
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, step_index)
);

CREATE TABLE leases (
    run_id       UUID PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
    owner        TEXT NOT NULL,         -- launcher instance id
    heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE scoped_credentials (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id        UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    credential    TEXT NOT NULL,        -- the scoped API key / token
    scope         TEXT NOT NULL,        -- e.g. 'llm:deepseek:chat' | 'github:read'
    expires_at    TIMESTAMPTZ NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX scoped_creds_run_idx ON scoped_credentials (run_id);
```

Rules:

- Queue claim = `UPDATE runs SET status='running' WHERE id = (SELECT id FROM runs
  WHERE status='queued' AND tenant_id = $1 ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED) RETURNING *`.
  Per-tenant queue ensures fairness across tenants.
- Runner heartbeats its lease every 5s. Launcher scans for leases older than 30s,
  deletes the dead container, sets run back to `queued` and increments `attempt`
  (fail permanently if `attempt >= max_attempts`). **This is the resume mechanism.**
- Credentials are minted at run creation time with a TTL = `max_attempts * 120s` and
  scoped to the specific LLM model + tools declared by the agent. The master API key
  never enters the sandbox.

---

## Phase 1 — Working MVP (target: ~1 week)

### Step 1.1 — Scaffold

**Tasks**
- Init repo with `uv init`, Python 3.12, add deps: fastapi, uvicorn, asyncpg,
  pydantic-ai, logfire, docker, pydantic-settings; dev deps: ruff, pytest, pytest-asyncio, httpx.
- `settings.py` with all env vars (DB URL, ANTHROPIC_API_KEY, LOGFIRE_TOKEN,
  AGENTBOX_API_TOKEN, RUNNER_IMAGE, MODEL_NAME, DEFAULT_TENANT_MAX_CONCURRENT).
- `docker-compose.yml` with Postgres 16 only (services added in later steps).
- Simple migration runner: apply `migrations/*.sql` in order, track in `schema_migrations` table.
- CI: GitHub Actions workflow running ruff + pytest.

**Acceptance**: `uv run pytest` passes (one placeholder test); `docker compose up -d postgres`
then `uv run python -m agentbox.db.migrate` applies 001 successfully.

### Step 1.2 — Control plane API

**Tasks**
- `POST /runs` → body `{agent_name, prompt, egress_allow?, tenant_id?}` → inserts
  queued run (default tenant if not specified) → **mints scoped credentials for
  this run** → returns `{id, status}`.
- `GET /runs/{id}` → run row incl. result/error/attempt/cost_estimate.
- `GET /runs/{id}/checkpoints` → list of checkpoints (step_index, kind, token_count,
  cost, created_at — not full payload).
- Bearer-token auth via `AGENTBOX_API_TOKEN` on all routes.
- Instrument app with `logfire.instrument_fastapi(app)` (no-op gracefully if LOGFIRE_TOKEN unset).

**Acceptance**: e2e test posts a run and reads it back with status `queued` and
a non-empty `scoped_credentials` entry.

### Step 1.3 — Durable execution layer (the core)

**Tasks** — implement `runner/durable.py`:
- `class DurableContext(run_id, pool)`: holds an in-memory step counter starting at 0.
- `async def step(kind: str, fingerprint: str, fn: Callable[[], Awaitable[JSONable]]) -> JSONable`:
  1. `idx = self.next_index()`
  2. SELECT checkpoint (run_id, idx). If found: verify fingerprint matches (log a
     warning on mismatch — non-determinism detected), return stored payload **without calling fn**.
  3. Else: `result = await fn()`, INSERT checkpoint (with token_count and cost if
     applicable), return result.
- Wrap Pydantic AI's model layer: implement a `DurableModel` that wraps any
  `pydantic_ai.models.Model` and routes its `request()` through `DurableContext.step("model_call", ...)`.
  Fingerprint = SHA256 of the serialized message list. Serialize model responses
  with Pydantic AI's message (de)serialization utilities so they round-trip through JSONB.
  **Design this wrapper as a reusable module that could be contributed back to
  pydantic-ai as a first-class plugin** — document the extension points.
- Tool durability: a `@durable_tool` decorator that wraps tool functions through
  `step("tool_call", ...)`.
- Unit tests with a fake model: run agent once (records N checkpoints), construct a
  fresh context, run again → model called **zero** times, same final answer.

**Acceptance**: unit tests prove replay works and never re-calls the model.

### Step 1.4 — Runner image & demo agent

**Tasks**
- `runner/credentials.py`: loader that reads scoped credentials from a well-known
  env var (`AGENTBOX_CREDENTIALS_JSON`) and makes them available to the agent.
  The agent never sees the master key.
- `runner/agents.py`: a demo "incident investigator" agent (Pydantic AI `Agent`) with
  2–3 tools, at least one slow tool (e.g. `analyze_logs` that sleeps ~10s and returns
  fake findings) so a run takes 30–60s — long enough to kill mid-run.
- `runner/main.py`: reads `RUN_ID` env var → loads run from DB → builds agent with
  `DurableModel` → heartbeats lease in a background task → executes → writes
  result/status + cost_estimate → exits 0/1. Configure `logfire` with `run_id`
  and `tenant_id` as span attributes.
- `docker/Dockerfile.runner`: python:3.12-slim, installs the package, entrypoint
  `python -m agentbox.runner.main`.

**Acceptance**: `docker run -e RUN_ID=... --network host` against local Postgres
completes a run end-to-end with a real model; `GET /runs/{id}` shows `succeeded`
plus a cost estimate; no master API key is visible inside the container.

### Step 1.5 — Launcher (Docker backend)

**Tasks**
- `launcher/worker.py`: loop — claim queued run (per-tenant: pass tenant_id) →
  create lease → start container via Docker SDK:
  - Image=RUNNER_IMAGE
  - Env: RUN_ID + DB URL + **scoped credentials** (instead of master key)
  - CPU limit 1.0, memory 512m, `pids_limit`, read-only rootfs, no extra capabilities
  → continue polling.
- Reaper loop: find leases with heartbeat older than 30s → force-remove container
  (label containers `agentbox.run_id=<id>` for lookup) → requeue or fail the run
  per attempt count. Also handle containers that exited non-zero without updating status.
- Add `control-plane` and `launcher` services to docker-compose.

**Acceptance**: `docker compose up` + `POST /runs` → run goes queued → running →
succeeded with no manual action. Scoped credentials are used, not the master key.

### Step 1.6 — Egress control

**Tasks**
- Add `egress-proxy` service (tinyproxy) on an `agentbox-internal` Docker network
  (`internal: true`, so no direct internet) plus an external network for the proxy itself.
- Proxy config: allowlist only `api.anthropic.com` + Postgres host + domains from
  the run's `egress_allow`. Simplest v1: per-run tinyproxy config is overkill —
  use a global allowlist file (api.anthropic.com only) and document per-run
  allowlists as a Phase 2 K8s NetworkPolicy feature.
- Launcher attaches runner containers ONLY to the internal network and sets
  `HTTP_PROXY`/`HTTPS_PROXY` envs pointing at the proxy.
- Demo tool `fetch_url` in the agent: fetching a non-allowlisted domain must fail.

**Acceptance**: e2e test — a run whose tool fetches `https://example.com` gets a
proxy denial; fetching the LLM API works. Runner container has no direct internet
(`docker exec ... curl https://example.com` fails).

### Step 1.7 — Kill-and-resume demo + tracing polish

**Tasks**
- `tests/e2e/test_kill_and_resume.py`: submit run → wait until ≥2 checkpoints
  exist → `docker kill` the runner container → assert: run returns to queued,
  relaunches, finishes `succeeded`, attempt==1, and total model-call count
  (count checkpoints with kind=model_call created AFTER the kill vs. total steps
  replayed) proves no step re-executed.
- Logfire: ensure one trace shows the full lifecycle; spans for enqueue, claim,
  container start, each model/tool call (replayed steps get a `replayed=true` attribute).
- `make demo` (or `just demo`) target that runs the kill-and-resume scenario and
  prints a timeline to the terminal.

**Acceptance**: the e2e test passes reliably 3 runs in a row. **Phase 1 done.**

---

## Phase 2 — Kubernetes + polish (target: ~1 week)

### Step 2.0 — Cold-start optimization

**Tasks**
- Implement **image layer caching** for K8s backend: pre-pull `RUNNER_IMAGE` on
  kind nodes at cluster start; document this as a DaemonSet in production.
- Add **warm pool** awareness: the launcher can keep 1–2 pre-initialized sandbox
  containers/pods ready for "hot" handoff — `AGENTBOX_WARM_POOL_SIZE` config.
  When a run is claimed, assign it to a warm pod if one is available.
- Measure and report `cold_start_ms` as a span attribute in Logfire.

**Acceptance**: submitting two runs in sequence shows the second run's cold-start
time is near-zero (warm pool). Documented in README with benchmark numbers.

### Step 2.1 — Kubernetes backend

- `k8s/`: kind cluster config, namespace `agentbox`, Job template (backoffLimit=0 —
  retries are OUR job, not K8s's), resource requests/limits, `ttlSecondsAfterFinished`,
  RBAC for the launcher (create/list/delete Jobs in one namespace).
- `launcher/backend_k8s.py` implementing the same backend interface as Docker
  (start / kill / is_alive), selected via `AGENTBOX_BACKEND=k8s|docker`.
- **Secret injection for K8s**: scoped credentials are injected as a temporary
  Kubernetes Secret (not as plain env vars in the Job manifest), with a cleanup
  after the Job completes.
- Postgres reachable from the cluster (host mapping via kind extraPortMappings, or run
  Postgres in-cluster via a simple manifest — pick one and document it).

**Acceptance**: same e2e suite passes with `AGENTBOX_BACKEND=k8s` (kill via
`kubectl delete pod`). Scoped credentials appear as a K8s Secret, not in the pod spec.

### Step 2.2 — Cost tracking

**Tasks**
- `cost/tracker.py`:
  - On each model call checkpoint, record `token_count` (from the LLM response) and
    compute an estimated cost using a configurable rate table (`COST_PER_1K_INPUT_TOKENS`,
    `COST_PER_1K_OUTPUT_TOKENS` per model).
  - Aggregate cost per run: `SUM(checkpoints.cost) + compute_time * compute_cost_per_second`.
  - Expose `GET /runs/{id}/cost` endpoint returning breakdown (input_tokens,
    output_tokens, llm_cost, compute_cost, total_estimated_usd).
- Cost tracking is best-effort (estimate), not a billing system.

**Acceptance**: a completed run returns a non-zero cost breakdown at `/runs/{id}/cost`.

### Step 2.3 — Quotas, fairness, cleanup

- Namespace `ResourceQuota` + `LimitRange` manifests.
- Launcher-side concurrency cap per tenant (`TENANT_MAX_CONCURRENT_RUNS`) and
  global max (`MAX_CONCURRENT_RUNS`). Simple fairness: claim oldest-first within
  each tenant queue, round-robin across tenants.
- NetworkPolicy: default-deny egress for runner pods; allow DNS + proxy only.
  Per-run extra domains go into the proxy allowlist dynamically (simple HTTP
  admin endpoint on a small proxy sidecar, or regenerate config per run — choose
  the simpler and document the tradeoff).

**Acceptance**: submitting 10 runs with MAX_CONCURRENT_RUNS=3 never exceeds 3
running pods; submitting runs across two tenants shows round-robin scheduling;
NetworkPolicy test shows direct egress blocked.

### Step 2.4 — gVisor (optional, config-level)

- `k8s/runtimeclass-gvisor.yaml` + docs: how to create a kind node with runsc,
  and `AGENTBOX_RUNTIME_CLASS=gvisor` toggle in the Job template.
- If runsc-in-kind proves flaky, keep the manifest + README section and mark it
  "tested on a GKE Sandbox node" honestly. Do not sink more than half a day here.

### Step 2.5 — MCP server (Logfire telemetry, not raw Postgres)

**Tasks**
- `mcp_server/server.py`: stdio MCP server exposing tools:
  - `list_runs(tenant_id?, status?, limit=10)` — queries **Logfire API** (using
    Logfire's export/query endpoint or OTel trace API) for run metadata.
  - `get_run_telemetry(run_id)` — queries Logfire for the full trace of a run:
    spans, duration per step, replay markers, token usage per model call.
  - `get_run_timeline(run_id)` — returns checkpoint timeline from Logfire traces.
- The MCP server does NOT read Postgres directly. It queries Logfire's OpenTelemetry
  trace data, which is the platform's single source of truth for observability.
  This mirrors Pydantic's own vision: *"agents query Logfire (via MCP) for live telemetry"*.
- Register the MCP toolset on a second demo agent ("ops assistant") that can answer
  "why did run X take so long?" — agents introspecting the platform via MCP.

**Acceptance**: scripted session where the ops agent answers a question about a
real past run using only MCP tools that source data from Logfire.

### Step 2.6 — README & demo assets

- README: problem statement (1 paragraph explicitly framed around running
  untrusted agent workloads), architecture diagram, quickstart
  (`docker compose up` path AND kind path), design notes (why replay-based
  durability; credential scoping approach; failure modes; what production would
  need: Firecracker, per-tenant credentials, fair scheduling).
- 30–60s terminal recording / GIF of the kill-and-resume demo.
- "Mapping to Pydantic's Agent Infrastructure Engineer JD" section — table:
  JD bullet → where in this repo it's implemented. Include all six JD points:
  sandboxing/credential scoping, scaling/cold-start/cost, durable execution,
  orchestration, Logfire + MCP observability, Pydantic AI deep integration.

---

## 6. Conventions for the implementing AI agent

1. Work step by step in the order above; keep commits small, one step ≈ 1–3 commits,
   Conventional Commits style.
2. After each step, run `ruff check`, `pytest`, and the step's acceptance check
   before moving on.
3. Prefer boring code: plain SQL, small modules, type hints everywhere, no clever
   metaprogramming. This repo is a writing sample.
4. Every subprocess/container interaction must have a timeout and a cleanup path.
5. Secrets only via env vars; never commit tokens; `.env.example` maintained.
6. **Master API keys NEVER enter the sandbox container** — only scoped credentials.
7. If a library API differs from what this plan assumes (esp. pydantic-ai message
   serialization), consult its current docs and adapt — the checkpoint/replay
   CONTRACT in Step 1.3 is fixed, the implementation details are not.
8. Cost tracking is best-effort estimation, not a billing system.

## 7. Definition of Done

- [ ] Kill-and-resume e2e passes on both Docker and K8s backends
- [ ] Egress default-deny verified by test
- [ ] **Scoped credentials**: verified that sandbox containers never have access to
      master API keys, and that scoped credentials expire after run TTL
- [ ] **Cost tracking**: completed runs show cost breakdown at `/runs/{id}/cost`
- [ ] **Cold-start optimization**: warm pool reduces second-run cold-start to near-zero;
      cold_start_ms span attribute recorded in Logfire
- [ ] **Multi-tenant data model**: tenant_id on all tables; per-tenant queue and
      concurrency limits working
- [ ] One Logfire trace shows a full run incl. replayed spans
- [ ] **MCP ops-agent queries Logfire** (not Postgres) and answers questions about
      past runs
- [ ] README with quickstart, diagram, JD-mapping table, demo GIF
- [ ] CI green
