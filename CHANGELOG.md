# Changelog

## 0.1.0 (2025-07-15)

### Initial MVP

- **Durable execution**: Checkpoint/replay for model calls and tool calls via Postgres
- **Sandboxing**: Docker containers with read-only rootfs, resource limits, egress control
- **Credential scoping**: Per-run token → real API key injection via credential proxy
- **Database RLS**: Row-level security for the runner database role
- **API**: FastAPI control plane with POST/GET runs, checkpoint listing, cost breakdown
- **Launcher**: Queue polling, per-tenant round-robin, dead lease reaper
- **Docker backend**: Container lifecycle management via Docker SDK
- **Kubernetes backend**: Job template, RBAC, NetworkPolicy, gVisor RuntimeClass
- **Observability**: Logfire spans for API, runner, and launcher operations
- **MCP server**: Agent telemetry query via Logfire SQL API
- **CI**: Ruff lint/format, pytest with coverage, pyright type checking, Docker build
