# Contributing to AgentBox

> **📚 See the full [Development Guide](docs/development.md) for detailed instructions on setup, testing, code style, and module deep-dives.**

## Quick Start

```bash
git clone https://github.com/shenxianpeng/agentbox.git
cd agentbox
uv sync --all-extras --dev

# Start Postgres
make postgres-up
make migrate

# Run tests
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

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
