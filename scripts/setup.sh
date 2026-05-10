#!/bin/bash
# Multi-Agent Studio - Project Setup Script
# Run this once after cloning the repository
# Requires: Python 3.10+ and Node.js 18+ (no Docker needed)

set -e

echo "=== Multi-Agent Studio Setup ==="
echo ""

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# 1. Check prerequisites
echo "[1/4] Checking prerequisites..."

command -v python3 >/dev/null 2>&1 || { echo "ERROR: python3 not installed"; exit 1; }
command -v node >/dev/null 2>&1 || { echo "ERROR: node not installed"; exit 1; }

echo "  python: $(python3 --version)"
echo "  node:   $(node --version)"

# 2. Install frontend dependencies
echo "[2/4] Installing frontend dependencies..."
cd "$PROJECT_ROOT/apps/web"
npm install 2>/dev/null || pnpm install 2>/dev/null || yarn install
cd "$PROJECT_ROOT"

# 3. Install Python dependencies
echo "[3/4] Installing Python dependencies..."
cd "$PROJECT_ROOT/apps/orchestrator"
pip install poetry 2>/dev/null || true
poetry install --no-root
cd "$PROJECT_ROOT"

# 4. Create SQLite data directory
echo "[4/4] Creating data directory..."
mkdir -p "$PROJECT_ROOT/apps/orchestrator/data"

echo ""
echo "=== Setup Complete ==="
echo ""
echo "To start development:"
echo "  ./scripts/dev.sh"
echo ""
echo "Or start services manually:"
echo "  Terminal 1: cd apps/orchestrator && poetry run python -m app.main"
echo "  Terminal 2: cd apps/web && npm run dev"
echo ""
echo "  Frontend: http://localhost:3000"
echo "  API Docs: http://localhost:8000/docs"
