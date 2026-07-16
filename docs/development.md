# Development

Guide for contributing to AgentBox.

---

## Setup

### Prerequisites

- Python 3.12+
- Docker + Docker Compose
- `uv` package manager

### Clone and install

```bash
git clone https://github.com/shenxianpeng/agentbox.git
cd agentbox
uv sync --all-extras --dev
```

### Start Postgres

```bash
make postgres-up
make migrate
```

## Running Tests

```bash
# All tests (requires Postgres)
make test

# Unit tests only (no Postgres needed)
make test-unit

# End-to-end tests (requires Postgres)
make test-e2e

# Linting
make lint
```

### Test Structure

```
tests/
├── unit/
│   ├── test_durable.py          # Core checkpoint/replay tests
│   ├── test_mcp_server.py       # MCP server tests
│   ├── test_cost.py             # Cost tracking tests
│   └── test_kill_and_resume.py  # Unit test with TestModel
└── e2e/
    ├── test_api.py              # API integration tests
    └── test_kill_and_resume.py  # Kill-and-resume demo test (requires Docker)
```

### Writing Tests

- Unit tests use an `InMemoryPool` to avoid requiring Postgres
- The `FakeModel` class in `test_durable.py` simulates an LLM
- Tests verify that replay fast-forwards without re-calling the model

```python
@pytest.mark.asyncio
async def test_replay_uses_checkpoints(pool, fake_model):
    """Verify replay uses cached checkpoints, not live calls."""
    run_id = "test-replay"
    context = DurableContext(run_id, pool)
    durable = DurableModel(fake_model, context)
    agent = Agent(durable, tools=[...], system_prompt="...")

    # First run: live execution
    result1 = await agent.run("Analyze...")

    # Second run: replay (model should NOT be called)
    context2 = DurableContext(run_id, pool)
    durable2 = DurableModel(fake_model, context2)
    agent2 = Agent(durable2, tools=[...], system_prompt="...")
    result2 = await agent2.run("Analyze...")

    assert model.call_count == 0  # No live calls during replay
    assert str(result2.output) == str(result1.output)  # Same output
```

## Code Style

- **Linter**: Ruff (`make lint`)
- **Line length**: 100 characters
- **Type hints**: Required for all public APIs
- **Async**: Use `async/await` throughout
- **Imports**: Use `from __future__ import annotations` in all modules

## Project Structure

```
src/agentbox/       # Main source code
├── api/            # FastAPI control plane
│   ├── main.py     # App setup, lifespan, middleware
│   └── routes.py   # API endpoints
├── cost/           # Cost estimation
│   └── tracker.py  # Token + compute cost calculations
├── db/             # Database layer
│   ├── migrate.py  # Migration runner
│   └── queries.py  # asyncpg query functions
├── launcher/       # Queue poller + sandbox backends
│   ├── worker.py           # Poll loop + reaper
│   ├── backend_docker.py   # Docker SDK backend
│   ├── backend_k8s.py      # Kubernetes backend
│   └── warm_pool.py        # Cold-start optimization
├── mcp_server/     # MCP server for telemetry
│   └── server.py
├── runner/         # Code inside the sandbox
│   ├── main.py         # Runner entrypoint (sets app.run_id via set_config)
│   ├── durable.py      # Core checkpoint/replay engine
│   ├── durable_model.py  # pydantic-ai Model wrapper
│   ├── durable_tool.py   # Tool checkpointing decorator
│   ├── agents.py        # Demo agent definitions
│   └── credentials.py   # Scoped credential loader
├── secrets/        # Credential scoping (per-run token minting)
│   └── scoper.py
└── settings.py     # pydantic-settings configuration
```

## Key Modules

### `durable.py` — Checkpoint/Replay Engine

The `DurableContext` class manages step-by-step checkpointing:

```python
context = DurableContext(run_id, pool)

# On first run: executes fn() and stores result
result = await context.step("model_call", fn, fingerprint=sha)

# On replay: returns stored result without calling fn()
result = await context.step("model_call", fn, fingerprint=sha)
```

The `step()` method uses a **single database connection** for both reading and writing checkpoints, ensuring atomicity.

### `durable_model.py` — Model Wrapper

`DurableModel` wraps any pydantic-ai `Model` and routes every `request()` through `DurableContext.step()`. This is a drop-in replacement:

```python
inner = OpenAIModel('gpt-4', api_key=key)
model = DurableModel(inner, context=durable_context)
agent = Agent(model)
```

During replay, stored checkpoint data (serialized as dicts) is reconstructed into proper `ModelResponse` objects via `_deserialize_model_response()`.

### `worker.py` — Launcher + Reaper

The launcher runs two concurrent loops:

- **Poll loop**: claims queued runs (round-robin across tenants) and starts containers
- **Reaper loop**: finds dead leases and requeues or fails runs

The `_handle_claimed_run()` method orchestrates lease creation, credential proxy registration, and container startup.

The launcher registers `{per_run_token → real_api_key}` with the credential proxy at `/admin/keys`. The per-run token is fetched from `scoped_credentials` (which stores only a UUID, NOT the master key). On container start failure, the key mapping is unregistered from the proxy.

## Making Changes

1. Create a feature branch from `main`.
2. Run `make lint && make test` locally.
3. Ensure all tests pass.
4. Open a PR with a clear description of the changes.

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
