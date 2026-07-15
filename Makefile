# =============================================================================
# AgentBox Makefile
# =============================================================================
# Groups: Build | Develop | Test | Docker | Demo | Utils
# =============================================================================

.PHONY: help build-runner build-controlplane dev-api dev-launcher migrate test test-unit test-e2e lint up down demo clean

# ── Build ────────────────────────────────────────────────────────────────────

build-runner:  ## Build the runner Docker image
	docker build -t agentbox-runner:latest -f docker/Dockerfile.runner .

build-controlplane:  ## Build the control-plane Docker image
	docker build -t agentbox-controlplane:latest -f docker/Dockerfile.controlplane .

# ── Develop ──────────────────────────────────────────────────────────────────

dev-api:  ## Start the API server locally with hot-reload
	uv run uvicorn agentbox.api.main:app --reload --host 0.0.0.0 --port 8000

dev-launcher:  ## Start the launcher worker locally
	uv run python -m agentbox.launcher.worker

migrate:  ## Run database migrations
	uv run python -m agentbox.db.migrate

# ── Test ─────────────────────────────────────────────────────────────────────

test:  ## Run all tests (unit + e2e); requires Postgres for e2e
	uv run pytest tests/ -v

test-unit:  ## Run unit tests only (no Postgres needed)
	uv run pytest tests/unit/ -v

test-e2e:  ## Run end-to-end tests only; requires Postgres
	uv run pytest tests/e2e/ -v

lint:  ## Run ruff linter on source and tests
	uv run ruff check src/ tests/

# ── Docker (full stack) ──────────────────────────────────────────────────────

up:  ## Start all services: Postgres + control-plane + launcher + egress-proxy
	docker compose down 2>/dev/null || true
	docker compose up -d
	@echo "Waiting for Postgres to be ready..."
	@for i in $$(seq 1 30); do \
		if docker compose exec -T postgres pg_isready -U agentbox >/dev/null 2>&1; then \
			break; \
		fi; \
		printf "."; \
		sleep 1; \
	done; echo ""
	@echo "Waiting for API server to be ready..."
	@for i in $$(seq 1 30); do \
		if curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/docs 2>/dev/null | grep -q "200\|404"; then \
			break; \
		fi; \
		printf "."; \
		sleep 1; \
	done; echo ""
	@echo "All services ready (migrations run automatically via control-plane entrypoint)."

down:  ## Stop all services and remove volumes
	docker compose down -v

postgres-up:  ## Start only Postgres (for local development)
	docker compose up -d postgres
	@echo "Waiting for Postgres..."
	@for i in $$(seq 1 30); do \
		if docker compose exec -T postgres pg_isready -U agentbox >/dev/null 2>&1; then \
			break; \
		fi; \
		printf "."; \
		sleep 1; \
	done; echo " ready!"

# ── Demo ──────────────────────────────────────────────────────────────────────

demo: build-runner up  ## Build runner image, start stack, then show demo instructions
	@echo ""
	@echo "==========================================="
	@echo "  AgentBox Kill-and-Resume Demo"
	@echo "==========================================="
	@echo ""
	@echo "Step 1: Launcher is already running inside Docker."
	@echo ""
	@echo "Step 2: Submitting run..."
	@curl -s -X POST http://localhost:8000/runs \
		-H "Authorization: Bearer $${AGENTBOX_API_TOKEN:-dev-token}" \
		-H "Content-Type: application/json" \
		-d '{"agent_name": "incident-investigator", "prompt": "Investigate the web service. Analyze logs and fetch metrics."}' | uv run python -m json.tool
	@echo ""
	@echo "Step 3: Wait for checkpoints, then kill container, observe resume..."
	@sleep 15
	@echo ""
	@echo "See tests/e2e/test_kill_and_resume.py for the automated version."
	@echo "==========================================="

# ── Utils ────────────────────────────────────────────────────────────────────

clean:  ## Remove cached and temporary files
	rm -rf .pytest_cache .ruff_cache
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

help:  ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| sort \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-24s\033[0m %s\n", $$1, $$2}'
