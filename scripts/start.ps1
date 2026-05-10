# Multi-Agent Studio - Start / Stop Services
# Usage:
#   .\scripts\start.ps1          # Start services
#   .\scripts\start.ps1 stop     # Stop services

param(
    [string]$Action = "start"
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$ProjectRoot = Split-Path -Parent $ScriptDir
$LogDir = Join-Path $ProjectRoot "logs"
$Ports = @(8000, 3000)

# ---------------------------------------------------------------------------
# Stop logic
# ---------------------------------------------------------------------------
function Stop-Services {
    $savedEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    Write-Host "=== Stopping Multi-Agent Studio ===" -ForegroundColor Cyan

    # Collect all PIDs occupying our ports
    $targetPids = @()
    foreach ($p in $Ports) {
        $pat = ":${p}\s"
        $lines = netstat -ano | Select-String $pat
        foreach ($line in $lines) {
            $parts = ($line -split '\s+') | Where-Object { $_ }
            $procId = $parts[-1]
            if ($procId -match '^\d+$' -and $procId -ne "0") {
                $targetPids += [int]$procId
            }
        }
    }

    $targetPids = $targetPids | Sort-Object -Unique

    if ($targetPids.Count -eq 0) {
        Write-Host "  No processes found on ports 8000/3000" -ForegroundColor Gray
    } else {
        foreach ($procId in $targetPids) {
            $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
            $name = if ($proc) { $proc.ProcessName } else { "unknown" }
            Write-Host "  Killing PID=${procId} (${name})" -ForegroundColor Yellow
            taskkill /F /T /PID $procId 2>$null | Out-Null
        }
    }

    # Kill orphan python processes running our app
    $pyProcs = Get-Process python* -ErrorAction SilentlyContinue |
        Where-Object {
            try {
                $cmdLine = (Get-CimInstance Win32_Process -Filter "ProcessId=$($_.Id)" -ErrorAction SilentlyContinue).CommandLine
                $cmdLine -and ($cmdLine -match "app\.main" -or $cmdLine -match "uvicorn" -or $cmdLine -match "orchestrator")
            } catch { $false }
        }

    foreach ($p in $pyProcs) {
        Write-Host "  Killing orphan python PID=$($p.Id)" -ForegroundColor Yellow
        Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
    }

    # Also kill any straggler processes on our ports
    $portPids = @()
    foreach ($p in $Ports) {
        $pat = ":${p}\s"
        $portPids += (netstat -ano | Select-String $pat) | ForEach-Object {
            ($_ -split '\s+')[-1]
        } | Where-Object { $_ -match '^\d+$' -and $_ -ne "0" }
    }
    $portPids = $portPids | Sort-Object -Unique
    foreach ($procId in $portPids) {
        Stop-Process -Id ([int]$procId) -Force -ErrorAction SilentlyContinue
    }

    # Kill orphan node processes running next dev
    $nodeProcs = Get-Process node* -ErrorAction SilentlyContinue |
        Where-Object {
            try {
                $cmdLine = (Get-CimInstance Win32_Process -Filter "ProcessId=$($_.Id)" -ErrorAction SilentlyContinue).CommandLine
                $cmdLine -and ($cmdLine -match "next" -or $cmdLine -match "apps\\web")
            } catch { $false }
        }

    foreach ($p in $nodeProcs) {
        Write-Host "  Killing orphan node PID=$($p.Id)" -ForegroundColor Yellow
        taskkill /F /T /PID $p.Id 2>$null | Out-Null
    }

    Start-Sleep -Seconds 1

    # Verify ports are free
    $clean = $true
    foreach ($p in $Ports) {
        $pat = ":${p}\s"
        $check = netstat -ano | Select-String $pat
        if ($check) {
            Write-Host "  [WARN] Port ${p} still occupied" -ForegroundColor Red
            $clean = $false
        } else {
            Write-Host "  Port ${p}: free" -ForegroundColor Green
        }
    }

    Write-Host ""
    if ($clean) {
        Write-Host "All services stopped." -ForegroundColor Green
    } else {
        Write-Host "Some processes may still be running. Try again or kill manually." -ForegroundColor Yellow
    }
    $ErrorActionPreference = $savedEAP
}

# ---------------------------------------------------------------------------
# Start logic
# ---------------------------------------------------------------------------
function Start-Services {
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
    $savedEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    foreach ($port in $Ports) {
        $pids = netstat -ano | Select-String ":$port\s" | ForEach-Object {
            ($_ -split '\s+')[-1]
        } | Where-Object { $_ -match '^\d+$' } | Sort-Object -Unique
        foreach ($procId in $pids) {
            if ($procId -and $procId -ne "0") {
                Write-Host "  Killing old process on port $port (PID=$procId)" -ForegroundColor Yellow
                taskkill /F /PID $procId 2>$null | Out-Null
            }
        }
    }
    $ErrorActionPreference = $savedEAP
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

    # Quick health check (1 attempt, don't block)
    Start-Sleep -Seconds 3

    $health = curl.exe -s http://localhost:8000/health 2>$null
    if ($health -match "ok") {
        Write-Host "  Backend:  OK  http://localhost:8000" -ForegroundColor Green
    } else {
        Write-Host "  Backend:  starting... see logs\orchestrator.log" -ForegroundColor Yellow
    }

    $feCode = curl.exe -s -o nul -w "%{http_code}" "http://localhost:3000" 2>$null
    if ($feCode -match "200|307") {
        Write-Host "  Frontend: OK  http://localhost:3000" -ForegroundColor Green
    } else {
        Write-Host "  Frontend: starting... see logs\frontend.log" -ForegroundColor Yellow
    }

    Write-Host ""
    Write-Host "Logs: $LogDir\" -ForegroundColor Gray
    Write-Host "Stop: .\scripts\start.ps1 stop" -ForegroundColor Gray
}

# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
if ($Action -eq "stop") {
    Stop-Services
} else {
    Start-Services
}
