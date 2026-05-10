# Multi-Agent Studio - Windows Dev Starter (PowerShell)
# Starts orchestrator (FastAPI) + frontend (Next.js)
# No Docker / Redis / Temporal required
# Usage: .\scripts\dev.ps1

$ErrorActionPreference = "Stop"

# Resolve project root relative to this script
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$ProjectRoot = Split-Path -Parent $ScriptDir
$LogDir = Join-Path $ProjectRoot "logs"

# Create log directory
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }

# Ensure SQLite data directory exists
$DataDir = Join-Path $ProjectRoot "apps\orchestrator\data"
if (-not (Test-Path $DataDir)) { New-Item -ItemType Directory -Path $DataDir -Force | Out-Null }

# Find poetry executable path
$PoetryExe = (Get-Command poetry -ErrorAction SilentlyContinue).Source
if (-not $PoetryExe) {
    $candidates = @(
        "$env:APPDATA\Python\Scripts\poetry.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python312\Scripts\poetry.exe"
    )
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
Write-Host "Starting services (no window popups, logs -> logs\)..." -ForegroundColor Yellow
Write-Host ""

# ---------------------------------------------------------------------------
# Helper: start a service process with NO window popup and full log capture.
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
# Start services (only orchestrator + frontend)
# ---------------------------------------------------------------------------
$procs = @()

$procs += Start-ServiceProcess -Name "orchestrator" `
    -Command "`"$PoetryExe`" run python -m app.main" `
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
# Health checks — also detect the actual frontend port (Next.js auto-increments)
# ---------------------------------------------------------------------------
$health = curl.exe -s http://localhost:8000/health 2>$null
if ($health -match "ok") {
    Write-Host ""
    Write-Host "  Backend:  OK" -ForegroundColor Green
} else {
    Write-Host ""
    Write-Host "  Backend:  starting... (check logs\orchestrator.log)" -ForegroundColor Yellow
}

$fePort = $null
foreach ($port in @(3000, 3001, 3002, 3003, 3004)) {
    $code = curl.exe -s -o nul -w "%{http_code}" "http://localhost:$port" 2>$null
    if ($code -match "200|307") {
        $fePort = $port
        break
    }
}

if ($fePort) {
    Write-Host "  Frontend: OK (port $fePort)" -ForegroundColor Green
} else {
    # Fallback: scan frontend log for the actual port line
    $feLog = Join-Path $LogDir "frontend.log"
    if (Test-Path $feLog) {
        $portMatch = Get-Content $feLog -Tail 20 | Select-String "Local:\s+http://localhost:(\d+)" | Select-Object -Last 1
        if ($portMatch -and $portMatch.Matches.Groups[1].Value) {
            $fePort = [int]$portMatch.Matches.Groups[1].Value
        }
    }
    if ($fePort) {
        Write-Host "  Frontend: starting on port $fePort..." -ForegroundColor Yellow
    } else {
        $fePort = 3000
        Write-Host "  Frontend: starting... (check logs\frontend.log)" -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host "=== All services started ===" -ForegroundColor Green
Write-Host ""
Write-Host "  Orchestrator:  http://localhost:8000" -ForegroundColor White
Write-Host "  Frontend:      http://localhost:$fePort" -ForegroundColor White
Write-Host ""
Write-Host "  Logs: $LogDir\" -ForegroundColor Gray
Write-Host ""
Write-Host "Press Enter to stop all services..." -ForegroundColor Yellow

# Wait for user to press Enter
$null = Read-Host

# ---------------------------------------------------------------------------
# Cleanup: kill the ENTIRE process tree
# ---------------------------------------------------------------------------
Write-Host "Stopping services..." -ForegroundColor Yellow

foreach ($p in $procs) {
    if ($p -and -not $p.HasExited) {
        $null = taskkill /T /F /PID $p.Id 2>$null
    }
}

Write-Host ""
Write-Host "All services stopped." -ForegroundColor Green
