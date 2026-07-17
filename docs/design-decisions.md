# Design Decisions

Architecture Decision Records for the choices most likely to be questioned.

---

## ADR-1: A Postgres checkpoint layer instead of Temporal / DBOS / Prefect

**Status**: accepted

Pydantic AI ships first-class durable-execution integrations for
[Temporal](https://ai.pydantic.dev/durable_execution/temporal/),
[DBOS](https://ai.pydantic.dev/durable_execution/dbos/), and
[Prefect](https://ai.pydantic.dev/durable_execution/prefect/). Why does
AgentBox implement its own replay layer on Postgres?

### Context

AgentBox already needs Postgres for its control plane: the run queue
(`FOR UPDATE SKIP LOCKED`), leases/heartbeats, tenants, and scoped
credential metadata all live there. The durability requirement is narrow
and precise: *a killed sandbox must resume without repeating a single
LLM call*.

### Decision

Checkpoint every side-effecting step (model call, tool call) to a
`checkpoints` table keyed by `(run_id, step_index)`, and replay by
fast-forwarding through existing checkpoints on restart.

### Rationale

- **No second system.** Temporal adds a server cluster plus worker
  processes; DBOS and Prefect add their own runtime and control plane.
  For a platform whose entire state already lives in one Postgres, a
  second source of truth increases the operational surface more than it
  saves code — the whole replay layer is ~200 lines.
- **The checkpoint table is also the product.** Checkpoints double as
  the audit log, the cost ledger (token counts and USD per step), and
  the data behind `GET /runs/{id}/checkpoints`. A workflow engine would
  keep this state internal, and we would re-export it anyway.
- **One isolation model.** Runner containers connect with a restricted
  role under Row-Level Security scoped to their `run_id`. Checkpoints
  written through the same connection inherit exactly the same isolation
  guarantees as everything else the runner touches.
- **Deterministic-replay contract is small here.** Agent loops are
  linear conversations: the N-th model call sees the same messages on
  replay (verified by fingerprint). We do not need Temporal's general
  workflow versioning, signals, or timers.

### Trade-offs accepted

- **At-least-once boundaries.** If the runner dies after an LLM call
  returns but before the checkpoint INSERT commits, that one in-flight
  call is repeated on resume. Temporal has the same fundamental window;
  we document it in [Failure Modes](architecture.md#failure-modes).
- **No cross-run orchestration.** Fan-out/fan-in across runs, timers,
  and human-in-the-loop signals would favor a workflow engine. If
  AgentBox grows those requirements, the right move is Pydantic AI's
  DBOS integration (closest to our "durability inside Postgres" shape),
  not more custom code.
- **Concurrent tool execution.** Step indices are assigned in call
  order. Agents that execute multiple tool calls concurrently could
  interleave differently on replay; the demo agent's tools run
  sequentially. A production version would key tool checkpoints by
  `(run_id, tool_name, args_fingerprint)` instead of a global counter.

---

## ADR-2: Credential proxy instead of vault-issued credentials

**Status**: accepted

The sandbox never receives a real LLM API key. It gets a random per-run
token; the credential proxy holds `{token → real key}` in memory only,
swaps the Authorization header on each request, and enforces the token's
TTL. Compromising the sandbox therefore yields a credential that expires
in minutes, works only through the proxy, and can be revoked centrally.

A vault (e.g. HashiCorp Vault) issuing short-lived downstream
credentials would be the production-grade version of the same idea, but
most LLM providers cannot mint scoped sub-keys — so a header-rewriting
proxy is what actually enforces the boundary either way.

---

## ADR-3: Egress allowlist is global, not per-run (known limitation)

**Status**: accepted (limitation documented)

`POST /runs` accepts an `egress_allow` list and stores it, but the
tinyproxy allowlist is a single static file shared by all runs — per-run
domains are **not** currently appended. Enforcing per-run policy needs
one of:

1. a proxy with a dynamic admin API (regenerate + reload per run),
2. per-run proxy sidecars, or
3. K8s NetworkPolicy objects generated per run.

All three are heavier than the MVP needs; the field stays in the API so
the data model doesn't change when enforcement lands. Until then, the
global allowlist in `docker/tinyproxy-allowlist.txt` is the boundary.
