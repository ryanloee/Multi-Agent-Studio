#!/bin/bash
# Multi-Agent Studio - Project Setup Script
# Run this once after cloning the repository

set -e

echo "=== Multi-Agent Studio Setup ==="

# 1. Check prerequisites
echo "[1/5] Checking prerequisites..."

command -v docker >/dev/null 2>&1 || { echo "ERROR: docker not installed"; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "ERROR: python3 not installed"; exit 1; }
command -v node >/dev/null 2>&1 || { echo "ERROR: node not installed"; exit 1; }

echo "  docker: $(docker --version)"
echo "  python: $(python3 --version)"
echo "  node:   $(node --version)"

# 2. Install frontend dependencies
echo "[2/5] Installing frontend dependencies..."
cd apps/web
npm install 2>/dev/null || pnpm install 2>/dev/null || yarn install
cd ../..

# 3. Install Python dependencies
echo "[3/5] Installing Python dependencies..."
cd apps/orchestrator
pip install poetry 2>/dev/null || true
poetry install
cd ../..

# 4. Build sandbox Docker image
echo "[4/5] Building sandbox base image..."
docker build -t multi-agent-studio/sandbox-base:latest infra/sandbox-images/base/

# 5. Start infrastructure
echo "[5/5] Starting infrastructure (PostgreSQL, Redis, Temporal, MinIO)..."
docker-compose -f infra/docker-compose.yml up -d

echo ""
echo "=== Setup Complete ==="
echo ""
echo "To start development:"
echo "  Python Orchestrator: cd apps/orchestrator && poetry run python -m app.main"
echo "  Frontend:            cd apps/web && npm run dev"
echo "  Temporal Worker:     cd apps/orchestrator && poetry run python -m app.workflows.worker"
echo "  Temporal UI:         http://localhost:8088"
echo "  MinIO Console:       http://localhost:9001"
