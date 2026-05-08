#!/bin/bash
# Multi-Agent Studio - Development Environment Starter
# Starts all services needed for local development

set -e

echo "=== Starting Multi-Agent Studio Dev Environment ==="

# Start infrastructure if not running
if ! docker ps | grep -q mas-postgres; then
    echo "Starting infrastructure..."
    docker-compose -f infra/docker-compose.yml up -d
    echo "Waiting for services to be ready..."
    sleep 5
fi

echo ""
echo "Starting services:"
echo "  1. Python Orchestrator (FastAPI) on :8000"
echo "  2. Temporal Worker"
echo "  3. Frontend (Next.js) on :3000"
echo ""

# Start Python orchestrator in background
cd apps/orchestrator
poetry run python -m app.main &
ORCHESTRATOR_PID=$!

# Start Temporal worker in background
poetry run python -m app.workflows.worker &
WORKER_PID=$!

cd ../..

# Start frontend
cd apps/web
npm run dev &
FRONTEND_PID=$!

cd ../..

echo ""
echo "All services started. Press Ctrl+C to stop."
echo "  Orchestrator: http://localhost:8000"
echo "  Frontend:     http://localhost:3000"
echo "  Temporal UI:  http://localhost:8088"
echo ""

# Wait for Ctrl+C
trap "echo 'Stopping...'; kill $ORCHESTRATOR_PID $WORKER_PID $FRONTEND_PID 2>/dev/null; exit 0" SIGINT SIGTERM
wait
