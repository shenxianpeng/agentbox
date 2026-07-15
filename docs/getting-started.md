# Getting Started

Set up AgentBox and run your first agent in under 5 minutes.

---

## Prerequisites

- Python 3.12+
- Docker + Docker Compose
- An LLM API key (DeepSeek or Anthropic)
- `uv` package manager

    ```bash
    curl -LsSf https://astral.sh/uv/install.sh | sh
    ```

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/shenxianpeng/agentbox.git
cd agentbox
```

### 2. Install dependencies

```bash
uv sync --all-extras --dev
```

### 3. Configure environment

```bash
cp .env.example .env
```

Edit `.env` and set at least one API key:

```env
# Required: at least one LLM API key
DEEPSEEK_API_KEY=sk-...
# or
ANTHROPIC_API_KEY=sk-ant-...

# Optional: Logfire observability
LOGFIRE_TOKEN=...

# Optional: override defaults
AGENTBOX_API_TOKEN=dev-token
MODEL_NAME=deepseek-chat
```

## Quickstart

### Start Postgres and run migrations

```bash
# Start Postgres (detached)
make postgres-up

# Run database migrations
make migrate
```

### Build the runner image

```bash
make build-runner
```

This builds the Docker image that runs inside the sandbox (with your agent code, not the full control plane).

### Start the launcher and API

In two separate terminals:

```bash
# Terminal 1: Start the launcher worker
make dev-launcher

# Terminal 2: Start the API server
make dev-api
```

### Submit a run

```bash
curl -X POST http://localhost:8000/runs \
  -H "Authorization: Bearer dev-token" \
  -H "Content-Type: application/json" \
  -d '{
    "agent_name": "incident-investigator",
    "prompt": "Investigate the web service. Analyze logs and fetch metrics."
  }'
```

You'll get a response with the run ID. Check its status:

```bash
curl -H "Authorization: Bearer dev-token" \
  http://localhost:8000/runs/<RUN_ID>
```

### Get cost breakdown

```bash
curl -H "Authorization: Bearer dev-token" \
  http://localhost:8000/runs/<RUN_ID>/cost
```

## Kill-and-Resume Demo

This is the signature feature of AgentBox. Run the automated test:

```bash
# Requires Docker + LLM API key
uv run pytest tests/e2e/test_kill_and_resume.py -v
```

The test:

1. Submits a run that takes ~30–60 seconds
2. Waits for ≥2 checkpoints
3. Force-kills the container
4. Verifies the run requeues, resumes, and completes with **zero repeated LLM calls**

Or use the Makefile demo target:

```bash
make demo
```

## Kubernetes (kind)

For running with Kubernetes instead of Docker:

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

## Makefile Reference

| Command | Purpose |
|---|---|
| `make build-runner` | Build the runner Docker image |
| `make build-controlplane` | Build the control-plane Docker image |
| `make dev-api` | Start API server locally with hot-reload |
| `make dev-launcher` | Start launcher worker locally |
| `make postgres-up` | Start only Postgres (for local dev) |
| `make up` | Start all services in Docker Compose |
| `make down` | Stop all services and remove volumes |
| `make migrate` | Run database migrations |
| `make test` | Run all tests |
| `make test-unit` | Run unit tests only |
| `make test-e2e` | Run end-to-end tests |
| `make lint` | Run ruff linter |
| `make demo` | Build, start, and run the kill-and-resume demo |
| `make clean` | Remove cached and temporary files |
