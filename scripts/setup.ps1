# Multi-Agent Studio - Windows Setup Script (PowerShell)
# Run this once after cloning the repository

$ErrorActionPreference = "Stop"

# Resolve project root relative to this script
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$ProjectRoot = Split-Path -Parent $ScriptDir

Write-Host "=== Multi-Agent Studio Setup ===" -ForegroundColor Cyan
Write-Host "Project root: $ProjectRoot" -ForegroundColor Gray
Write-Host ""

# 1. Check prerequisites
Write-Host "[1/5] Checking prerequisites..." -ForegroundColor Yellow

$missing = @()

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    $missing += "docker"
} else {
    Write-Host "  docker: $(docker --version)" -ForegroundColor Green
}

if (-not (Get-Command python -ErrorAction SilentlyContinue) -and -not (Get-Command python3 -ErrorAction SilentlyContinue)) {
    $missing += "python"
} else {
    $py = if (Get-Command python3 -ErrorAction SilentlyContinue) { "python3" } else { "python" }
    Write-Host "  python: $(& $py --version)" -ForegroundColor Green
}

if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    $missing += "node"
} else {
    Write-Host "  node:   $(node --version)" -ForegroundColor Green
}

if (-not (Get-Command pnpm -ErrorAction SilentlyContinue)) {
    if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
        $missing += "pnpm or npm"
    } else {
        Write-Host "  pnpm: not found, will use npm" -ForegroundColor Yellow
    }
} else {
    Write-Host "  pnpm:  $(pnpm --version)" -ForegroundColor Green
}

if ($missing.Count -gt 0) {
    Write-Host "`nERROR: Missing required tools: $($missing -join ', ')" -ForegroundColor Red
    Write-Host "Please install them first:" -ForegroundColor Red
    foreach ($m in $missing) {
        switch ($m) {
            "docker" { Write-Host "  - Docker Desktop: https://docs.docker.com/desktop/install/windows-install/" }
            "python" { Write-Host "  - Python 3.11+: https://www.python.org/downloads/" }
            "node"   { Write-Host "  - Node.js 18+: https://nodejs.org/" }
        }
    }
    exit 1
}

# 2. Install frontend dependencies
Write-Host "[2/5] Installing frontend dependencies..." -ForegroundColor Yellow
Set-Location "$ProjectRoot\apps\web"
if (Get-Command pnpm -ErrorAction SilentlyContinue) {
    pnpm install
} else {
    npm install
}
Set-Location $ProjectRoot

# 3. Install Python dependencies
Write-Host "[3/5] Installing Python dependencies..." -ForegroundColor Yellow
Set-Location "$ProjectRoot\apps\orchestrator"

if (-not (Get-Command poetry -ErrorAction SilentlyContinue)) {
    Write-Host "  Installing poetry..." -ForegroundColor Yellow
    pip install poetry
}

# Detect poetry command
$PoetryCmd = "poetry"
$PoetryArgs = @()
if (-not (Get-Command poetry -ErrorAction SilentlyContinue)) {
    $PoetryCmd = "python"
    $PoetryArgs = @("-m", "poetry")
}

# Fix pyproject.toml if missing README.md
if (-not (Test-Path "README.md")) {
    Write-Host "  Creating README.md for poetry..." -ForegroundColor Yellow
    "" | Out-File -FilePath "README.md" -Encoding utf8
}

& $PoetryCmd ($PoetryArgs + @("install", "--no-root"))
Set-Location $ProjectRoot

# 4. Build sandbox Docker image
Write-Host "[4/5] Building sandbox base image (this may take 5-10 minutes)..." -ForegroundColor Yellow
docker build -t multi-agent-studio/sandbox-base:latest "$ProjectRoot\infra\sandbox-images\base"

# 5. Start infrastructure
Write-Host "[5/5] Starting infrastructure (PostgreSQL, Redis, Temporal, MinIO)..." -ForegroundColor Yellow
docker compose -f "$ProjectRoot\infra\docker-compose.yml" up -d

Write-Host ""
Write-Host "=== Setup Complete ===" -ForegroundColor Green
Write-Host ""
Write-Host "To start development, run:" -ForegroundColor Cyan
Write-Host "  .\scripts\dev.ps1" -ForegroundColor White
Write-Host ""
Write-Host "Or start services manually in separate terminals:" -ForegroundColor Cyan
Write-Host "  Terminal 1: cd apps\orchestrator ; poetry run python -m app.main" -ForegroundColor White
Write-Host "  Terminal 2: cd apps\orchestrator ; poetry run python -m app.workflows.worker" -ForegroundColor White
Write-Host "  Terminal 3: cd apps\web ; pnpm dev" -ForegroundColor White
Write-Host ""
Write-Host "  Frontend:     http://localhost:3000" -ForegroundColor White
Write-Host "  API Docs:     http://localhost:8000/docs" -ForegroundColor White
Write-Host "  Temporal UI:  http://localhost:8088" -ForegroundColor White
