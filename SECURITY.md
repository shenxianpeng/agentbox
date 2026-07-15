# Security Policy

## Supported Versions

| Version | Supported          |
|---------|--------------------|
| 0.1.x   | :white_check_mark: |

## Reporting a Vulnerability

AgentBox is a portfolio/demo project. If you discover a security vulnerability,
please open a [GitHub Issue](https://github.com/shenxianpeng/agentbox/issues)
with the label `security`. Do not disclose the vulnerability publicly until it
has been addressed.

## Threat Model

AgentBox runs untrusted AI agent workloads in isolated sandboxes.
The following security properties are design goals:

### In Scope

- **Credential isolation**: The sandbox should never have access to master API
  keys. See [Credential Proxy](docker/credential_proxy.py) for the
  per-run token → real key injection mechanism.
- **Network egress control**: Sandbox containers can only reach allowlisted
  domains via the egress proxy. See [Egress Proxy](docker/tinyproxy.conf).
- **Database access control**: The runner connects with a restricted database
  role (`agentbox_runner`) that uses Row-Level Security (RLS) to limit access
  to its own run's data. See [RLS migration](migrations/002_runner_rls.sql).
- **Container isolation**: Runner containers run as non-root, with read-only
  rootfs, resource limits, and no extra capabilities.

### Out of Scope (MVP)

- **Side-channel attacks**: No protection against timing attacks, cache timing,
  or other side-channel information leaks.
- **Kernel-level isolation**: No gVisor/Firecracker/Kata Containers.
  Container isolation relies on Docker's default security features.
- **Persistence / storage encryption**: Checkpoint data in Postgres is not
  encrypted at rest. This is acceptable for local/demo use.
- **Denial of service**: No per-tenant rate limiting or resource quota
  enforcement at the platform level (Docker resource limits per container are
  enforced).

### Known Security Considerations

1. The credential proxy stores API keys in memory. If the proxy process is
   compromised, all active keys are exposed.
2. The `agentbox_runner` database role has SELECT permission on all runs
   (needed for validation). Consider narrowing to per-run RLS only in
   production.
3. The egress proxy (tinyproxy) uses `FilterDefaultDeny Yes` but does not
   support per-run domain allowlists dynamically. This is a Phase 2 feature.
