# Multi-Agent Studio - Windows Dev Starter (PowerShell)
# Starts all services needed for local development
# Usage: .\scripts\dev.ps1

$ErrorActionPreference = "Stop"

# Resolve project root relative to this script
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$ProjectRoot = Split-Path -Parent $ScriptDir
$LogDir = Join-Path $ProjectRoot "logs"
$BatDir = Join-Path $ProjectRoot ".dev-bat"

# Create directories
foreach ($d in @($LogDir, $BatDir)) {
    if (-not (Test-Path $d)) { New-Item -ItemType Directory -Path $d -Force | Out-Null }
}

# Find poetry executable path
$PoetryExe = (Get-Command poetry -ErrorAction SilentlyContinue).Source
if (-not $PoetryExe) {
    # Try common locations
    $candidates = @(
        "$env:APPDATA\Python\Scripts\poetry.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python312\Scripts\poetry.exe"
    )
    # Also search AppData\Local\Packages for Microsoft Store Python
    $found = Get-ChildItem -Path "$env:LOCALAPPDATA\Packages" -Filter "poetry.exe" -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($found) { $PoetryExe = $found.FullName }
    foreach ($c in $candidates) {
        if (Test-Path $c) { $PoetryExe = $c; break }
    }
}
if (-not $PoetryExe) {
    Write-Host "ERROR: poetry not found. Install with: pip install poetry" -ForegroundColor Red
    exit 1
}
Write-Host "Poetry: $PoetryExe" -ForegroundColor Gray

Write-Host "=== Starting Multi-Agent Studio Dev Environment ===" -ForegroundColor Cyan
Write-Host "Project root: $ProjectRoot" -ForegroundColor Gray
Write-Host ""

# Check if Docker is running
try {
    $null = docker ps 2>&1
} catch {
    Write-Host "Docker is not running or not responding. Please start Docker Desktop first." -ForegroundColor Red
    exit 1
}

# Start infrastructure if not running
$pgRunning = docker ps --format "{{.Names}}" 2>&1 | Select-String "mas-postgres" -Quiet
if (-not $pgRunning) {
    Write-Host "Starting infrastructure containers..." -ForegroundColor Yellow
    Push-Location $ProjectRoot
    docker compose -f infra/docker-compose.yml up -d
    Pop-Location
    Write-Host "Waiting for services to be ready..." -ForegroundColor Yellow
    Start-Sleep -Seconds 8
} else {
    Write-Host "Infrastructure containers already running." -ForegroundColor Green
}

Write-Host ""
Write-Host "Starting services (no window popups, logs -> logs\)..." -ForegroundColor Yellow
Write-Host ""

# Helper: create a .bat launcher and run it hidden (no window)
function Start-Service {
    param([string]$Name, [string]$BatContent)

    $batFile = Join-Path $BatDir "$Name.bat"
    $logFile = Join-Path $LogDir "$Name.log"

    # Write batch file
    @"
@echo off
cd /d "$ProjectRoot"
$BatContent > "$logFile" 2>&1
"@ | Out-File -FilePath $batFile -Encoding ascii -Force

    # Start hidden (no window) via start command with /min
    $proc = Start-Process -FilePath "cmd.exe" `
        -ArgumentList "/c", "start `"$Name`" /min `"$batFile`"" `
        -PassThru -WindowStyle Hidden

    Write-Host "  [$Name] PID=$($proc.Id)" -ForegroundColor Gray
    return $proc
}

# Start services
$procs = @()

$procs += Start-Service -Name "orchestrator" -BatContent "cd /d apps\orchestrator && `"$PoetryExe`" run python -m app.main"
$procs += Start-Service -Name "worker" -BatContent "cd /d apps\orchestrator && `"$PoetryExe`" run python -m app.workflows.worker"

if (Get-Command pnpm -ErrorAction SilentlyContinue) {
    $procs += Start-Service -Name "frontend" -BatContent "cd /d apps\web && pnpm dev"
} else {
    $procs += Start-Service -Name "frontend" -BatContent "cd /d apps\web && npm run dev"
}

# Give services a moment to start
Start-Sleep -Seconds 3

# Quick health check (use curl.exe, not PowerShell's curl alias)
$health = curl.exe -s http://localhost:8000/health 2>$null
if ($health -match "ok") {
    Write-Host ""
    Write-Host "  Backend: OK" -ForegroundColor Green
} else {
    Write-Host ""
    Write-Host "  Backend: starting... (check logs\orchestrator.log)" -ForegroundColor Yellow
}

$feCode = curl.exe -s -o /dev/null -w "%{http_code}" http://localhost:3000 2>$null
if ($feCode -match "200|307") {
    Write-Host "  Frontend: OK" -ForegroundColor Green
} else {
    Write-Host "  Frontend: starting... (check logs\frontend.log)" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "=== All services started ===" -ForegroundColor Green
Write-Host ""
Write-Host "  Orchestrator:  http://localhost:8000" -ForegroundColor White
Write-Host "  API Docs:      http://localhost:8000/docs" -ForegroundColor White
Write-Host "  Frontend:      http://localhost:3000" -ForegroundColor White
Write-Host "  Temporal UI:   http://localhost:8088" -ForegroundColor White
Write-Host ""
Write-Host "  Logs: $LogDir\" -ForegroundColor Gray
Write-Host ""
Write-Host "Press Enter to stop all services and containers..." -ForegroundColor Yellow

# Wait for user to press Enter
$null = Read-Host

# Cleanup: kill all background processes
Write-Host "Stopping services..." -ForegroundColor Yellow

foreach ($p in $procs) {
    try { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue } catch {}
}

# Clean up batch files
Remove-Item -Path $BatDir -Recurse -Force -ErrorAction SilentlyContinue

# Stop Docker containers
Write-Host "Stopping Docker containers..." -ForegroundColor Yellow
Push-Location $ProjectRoot
docker compose -f infra/docker-compose.yml down
Pop-Location

Write-Host ""
Write-Host "All services and containers stopped." -ForegroundColor Green
