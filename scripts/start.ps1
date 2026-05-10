# Multi-Agent Studio - Start Services
# Usage: .\scripts\start.ps1

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$ProjectRoot = Split-Path -Parent $ScriptDir
$LogDir = Join-Path $ProjectRoot "logs"

if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }

# Find poetry
$PoetryExe = (Get-Command poetry -ErrorAction SilentlyContinue).Source
if (-not $PoetryExe) {
    $found = Get-ChildItem -Path "$env:LOCALAPPDATA\Packages" -Filter "poetry.exe" -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($found) { $PoetryExe = $found.FullName }
}
if (-not $PoetryExe) {
    Write-Host "[ERROR] poetry not found" -ForegroundColor Red
    exit 1
}

Write-Host "=== Multi-Agent Studio ===" -ForegroundColor Cyan
Write-Host ""

# Kill any existing processes on our ports first
foreach ($port in @(8000, 3000)) {
    $pids = netstat -ano | Select-String ":$port\s" | ForEach-Object {
        ($_ -split '\s+')[-1]
    } | Where-Object { $_ -match '^\d+$' } | Sort-Object -Unique
    foreach ($pid in $pids) {
        if ($pid -and $pid -ne "0") {
            Write-Host "  Killing old process on port $port (PID=$pid)" -ForegroundColor Yellow
            taskkill /F /PID $pid 2>$null | Out-Null
        }
    }
}
Start-Sleep -Seconds 1

# Start backend
Write-Host "  Starting backend (port 8000)..." -ForegroundColor Gray
$backendLog = Join-Path $LogDir "orchestrator.log"
$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = "cmd.exe"
$psi.Arguments = "/c cd /d `"$ProjectRoot\apps\orchestrator`" && `"$PoetryExe`" run python -m app.main > `"$backendLog`" 2>&1"
$psi.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
$psi.CreateNoWindow = $true
$psi.UseShellExecute = $false
$backendProc = [System.Diagnostics.Process]::Start($psi)
Write-Host "    PID=$($backendProc.Id)" -ForegroundColor DarkGray

# Start frontend
Write-Host "  Starting frontend (port 3000)..." -ForegroundColor Gray
$frontendLog = Join-Path $LogDir "frontend.log"
$pnpm = (Get-Command pnpm -ErrorAction SilentlyContinue).Source
$devCmd = if ($pnpm) { "pnpm dev" } else { "npm run dev" }

$psi2 = New-Object System.Diagnostics.ProcessStartInfo
$psi2.FileName = "cmd.exe"
$psi2.Arguments = "/c cd /d `"$ProjectRoot\apps\web`" && $devCmd > `"$frontendLog`" 2>&1"
$psi2.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
$psi2.CreateNoWindow = $true
$psi2.UseShellExecute = $false
$frontendProc = [System.Diagnostics.Process]::Start($psi2)
Write-Host "    PID=$($frontendProc.Id)" -ForegroundColor DarkGray

Write-Host ""

# Health check
Start-Sleep -Seconds 5

$ok = $false
for ($i = 0; $i -lt 3; $i++) {
    $health = curl.exe -s http://localhost:8000/health 2>$null
    if ($health -match "ok") { $ok = $true; break }
    Start-Sleep -Seconds 2
}

if ($ok) {
    Write-Host "  Backend:  OK  http://localhost:8000" -ForegroundColor Green
} else {
    Write-Host "  Backend:  check logs\orchestrator.log" -ForegroundColor Yellow
}

$feCode = curl.exe -s -o nul -w "%{http_code}" "http://localhost:3000" 2>$null
if ($feCode -match "200|307") {
    Write-Host "  Frontend: OK  http://localhost:3000" -ForegroundColor Green
} else {
    Write-Host "  Frontend: check logs\frontend.log" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Logs: $LogDir\" -ForegroundColor Gray
Write-Host "Stop: .\scripts\stop.ps1" -ForegroundColor Gray
