#!/bin/sh
set -e

# Activate the uv-managed virtual environment
. /app/.venv/bin/activate

# Run database migrations
python -m agentbox.db.migrate

# Start the API server
exec uvicorn agentbox.api.main:app --host 0.0.0.0 --port 8000
