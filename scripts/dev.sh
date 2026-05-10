#!/bin/bash
# Multi-Agent Studio - Development Environment Starter
# Starts orchestrator (FastAPI) + frontend (Next.js)
# No Docker / Redis / Temporal required

set -e

echo "=== Starting Multi-Agent Studio Dev Environment ==="
echo ""
echo "Starting services:"
echo "  1. Python Orchestrator (FastAPI) on :8000"
echo "  2. Frontend (Next.js) on :3000"
echo ""

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# Ensure SQLite data directory exists
mkdir -p "$PROJECT_ROOT/apps/orchestrator/data"

# Start Python orchestrator in background
cd "$PROJECT_ROOT/apps/orchestrator"
python3 -m app.main &
ORCHESTRATOR_PID=$!

cd "$PROJECT_ROOT"

# Start frontend
cd "$PROJECT_ROOT/apps/web"
npm run dev 2>/dev/null || pnpm dev 2>/dev/null || yarn dev &
FRONTEND_PID=$!

cd "$PROJECT_ROOT"

echo ""
echo "All services started. Press Ctrl+C to stop."
echo "  Orchestrator: http://localhost:8000"
echo "  API Docs:     http://localhost:8000/docs"
echo "  Frontend:     http://localhost:3000"
echo ""

# Wait for Ctrl+C
trap "echo 'Stopping...'; kill $ORCHESTRATOR_PID $FRONTEND_PID 2>/dev/null; exit 0" SIGINT SIGTERM
wait
