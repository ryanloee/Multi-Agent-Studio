# Multi-Agent Studio - Windows Dev Starter (PowerShell)
# Starts all services needed for local development
# Usage: .\scripts\dev.ps1

$ErrorActionPreference = "Stop"

# Resolve project root relative to this script
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$ProjectRoot = Split-Path -Parent $ScriptDir
$LogDir = Join-Path $ProjectRoot "logs"

# Create log directory
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }

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

# ---------------------------------------------------------------------------
# Start infrastructure containers (create if missing, start if stopped, skip if running)
# ---------------------------------------------------------------------------
$pgRunning = docker ps --format "{{.Names}}" 2>$null | Select-String "mas-postgres" -Quiet
$pgExists  = docker ps -a --format "{{.Names}}" 2>$null | Select-String "mas-postgres" -Quiet

if (-not $pgExists) {
    Write-Host "Creating infrastructure containers..." -ForegroundColor Yellow
    Push-Location $ProjectRoot
    docker compose -f infra/docker-compose.yml up -d
    Pop-Location
    Write-Host "Waiting for services to be ready..." -ForegroundColor Yellow
    Start-Sleep -Seconds 8
} elseif (-not $pgRunning) {
    Write-Host "Starting infrastructure containers..." -ForegroundColor Yellow
    Push-Location $ProjectRoot
    docker compose -f infra/docker-compose.yml start
    Pop-Location
    Write-Host "Waiting for services to be ready..." -ForegroundColor Yellow
    Start-Sleep -Seconds 8
} else {
    Write-Host "Infrastructure containers already running." -ForegroundColor Green
}

Write-Host ""
Write-Host "Starting services (no window popups, logs -> logs\)..." -ForegroundColor Yellow
Write-Host ""

# ---------------------------------------------------------------------------
# Helper: start a service process with NO window popup and full log capture.
# Uses .NET ProcessStartInfo so CreateNoWindow works reliably.
# Returns the REAL process object (the cmd.exe that runs your command).
# ---------------------------------------------------------------------------
function Start-ServiceProcess {
    param(
        [string]$Name,
        [string]$Command,
        [string]$WorkingDir
    )

    $logFile = Join-Path $LogDir "$Name.log"

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = "cmd.exe"
    # /k keeps cmd alive while child runs; we kill it later with taskkill /T
    $psi.Arguments = "/k $Command > `"$logFile`" 2>&1"
    $psi.WorkingDirectory = $WorkingDir
    $psi.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
    $psi.CreateNoWindow = $true
    $psi.UseShellExecute = $false

    $proc = [System.Diagnostics.Process]::Start($psi)
    Write-Host "  [$Name] PID=$($proc.Id)" -ForegroundColor Gray
    return $proc
}

# ---------------------------------------------------------------------------
# Start services
# ---------------------------------------------------------------------------
$procs = @()

$procs += Start-ServiceProcess -Name "orchestrator" `
    -Command "`"$PoetryExe`" run python -m app.main" `
    -WorkingDir "$ProjectRoot\apps\orchestrator"

$procs += Start-ServiceProcess -Name "worker" `
    -Command "`"$PoetryExe`" run python -m app.workflows.worker" `
    -WorkingDir "$ProjectRoot\apps\orchestrator"

if (Get-Command pnpm -ErrorAction SilentlyContinue) {
    $procs += Start-ServiceProcess -Name "frontend" `
        -Command "pnpm dev" `
        -WorkingDir "$ProjectRoot\apps\web"
} else {
    $procs += Start-ServiceProcess -Name "frontend" `
        -Command "npm run dev" `
        -WorkingDir "$ProjectRoot\apps\web"
}

# Give services a moment to start
Start-Sleep -Seconds 4

# ---------------------------------------------------------------------------
# Health checks
# ---------------------------------------------------------------------------
$health = curl.exe -s http://localhost:8000/health 2>$null
if ($health -match "ok") {
    Write-Host ""
    Write-Host "  Backend:  OK" -ForegroundColor Green
} else {
    Write-Host ""
    Write-Host "  Backend:  starting... (check logs\orchestrator.log)" -ForegroundColor Yellow
}

$feCode = curl.exe -s -o nul -w "%{http_code}" http://localhost:3000 2>$null
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
Write-Host "Press Enter to stop all services (containers will be kept)..." -ForegroundColor Yellow

# Wait for user to press Enter
$null = Read-Host

# ---------------------------------------------------------------------------
# Cleanup: kill the ENTIRE process tree (cmd + all children: node, python...)
# ---------------------------------------------------------------------------
Write-Host "Stopping services..." -ForegroundColor Yellow

foreach ($p in $procs) {
    if ($p -and -not $p.HasExited) {
        # /T = kill tree (children too), /F = force
        $null = taskkill /T /F /PID $p.Id 2>$null
    }
}

# Stop Docker containers (keep them — do NOT remove)
Write-Host "Stopping Docker containers (keeping data)..." -ForegroundColor Yellow
Push-Location $ProjectRoot
docker compose -f infra/docker-compose.yml stop
Pop-Location

Write-Host ""
Write-Host "All services stopped. Containers are preserved for next start." -ForegroundColor Green
