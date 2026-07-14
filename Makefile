.PHONY: help demo build-runner up down test lint migrate clean

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

build-runner:  ## Build the runner Docker image
	docker build -t agentbox-runner:latest -f docker/Dockerfile.runner .

build-controlplane:  ## Build the control-plane Docker image
	docker build -t agentbox-controlplane:latest -f docker/Dockerfile.controlplane .

up:  ## Start all services (Postgres + control-plane + launcher + egress-proxy)
	docker compose up -d
	@echo "Waiting for Postgres..."
	@sleep 5
	@echo "Running migrations..."
	uv run python -m agentbox.db.migrate

down:  ## Stop all services
	docker compose down -v

migrate:  ## Run database migrations
	uv run python -m agentbox.db.migrate

test:  ## Run all tests
	uv run pytest tests/ -v

test-unit:  ## Run unit tests only
	uv run pytest tests/unit/ -v

test-e2e:  ## Run e2e tests (requires Postgres)
	uv run pytest tests/e2e/ -v

lint:  ## Run ruff linter
	uv run ruff check src/ tests/

demo: build-runner up  ## Run the kill-and-resume demo
	@echo ""
	@echo "==========================================="
	@echo "  AgentBox Kill-and-Resume Demo"
	@echo "==========================================="
	@echo ""
	@echo "Step 1: Starting launcher..."
	@uv run python -m agentbox.launcher.worker &
	@sleep 2
	@echo ""
	@echo "Step 2: Submitting run..."
	@curl -s -X POST http://localhost:8000/runs \
		-H "Authorization: Bearer ${AGENTBOX_API_TOKEN:-dev-token}" \
		-H "Content-Type: application/json" \
		-d '{
			"agent_name": "incident-investigator",
			"prompt": "Investigate the web service. Analyze logs and fetch metrics."
		}' | python -m json.tool
	@echo ""
	@echo "Step 3: Wait for checkpoints, then kill container, observe resume..."
	@sleep 15
	@echo ""
	@echo "See test_kill_and_resume.py for the automated version."
	@echo "==========================================="

clean:  ## Clean up temporary files
	rm -rf .pytest_cache .ruff_cache
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

api:  ## Start the API server locally (for development)
	uv run uvicorn agentbox.api.main:app --reload --host 0.0.0.0 --port 8000

launcher:  ## Start the launcher locally (for development)
	uv run python -m agentbox.launcher.worker
