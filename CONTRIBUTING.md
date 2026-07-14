# Contributing to AgentBox

## Development Setup

1. **Prerequisites**: Python 3.12+, Docker, `uv` package manager.

2. **Clone and install**:

   ```bash
   git clone https://github.com/shenxianpeng/agentbox.git
   cd agentbox
   uv sync --all-extras --dev
   ```

3. **Start Postgres**:

   ```bash
   docker compose up -d postgres
   uv run python -m agentbox.db.migrate
   ```

4. **Run tests**:

   ```bash
   make test        # all tests
   make test-unit   # unit tests only
   make lint        # linting
   ```

## Code Style

- **Linting**: Ruff (`make lint`)
- **Line length**: 100
- **Type hints**: Required for all public APIs
- **Async**: Use `async/await` throughout

## Pull Request Process

1. Create a feature branch from `main`.
2. Run `make lint && make test` locally.
3. Open a PR with a clear description of the changes.
4. Ensure CI passes.

## Project Structure

```
src/agentbox/       # Main source code
  ├── api/          # FastAPI control plane
  ├── db/           # Database layer
  ├── launcher/     # Queue poller + sandbox backends
  ├── runner/       # Code inside the sandbox
  ├── mcp_server/   # MCP server
  ├── secrets/      # Credential scoping
  ├── cost/         # Cost tracking
  └── settings.py   # Configuration
tests/              # Tests
  ├── unit/         # Unit tests
  └── e2e/          # Integration tests
```

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
