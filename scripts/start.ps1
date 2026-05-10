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
$PidDir = Join-Path $ProjectRoot ".pid"

# ---------------------------------------------------------------------------
# Helper: kill a PID and ALL its descendant processes recursively
# ---------------------------------------------------------------------------
function Kill-ProcessTree {
    param([int]$ParentPid)

    $ErrorActionPreference = "Continue"
    try {
        # Find all children recursively
        $descendants = @()
        $queue = @($ParentPid)
        while ($queue.Count -gt 0) {
            $current = $queue[0]
            $queue = $queue[1..($queue.Count - 1)]
            $kids = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
                Where-Object { $_.ParentProcessId -eq $current -and $_.ProcessId -ne $current }
            foreach ($kid in $kids) {
                if ($descendants -notcontains $kid.ProcessId) {
                    $descendants += $kid.ProcessId
                    $queue += $kid.ProcessId
                }
            }
        }

        # Kill descendants first (children before parents), then the parent
        $allToKill = ($descendants | Sort-Object -Descending) + @($ParentPid) | Select-Object -Unique
        foreach ($procId in $allToKill) {
            $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
            if ($proc) {
                Write-Host "    Killing PID=$procId ($($proc.ProcessName))" -ForegroundColor DarkGray
                Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
            }
        }
    } catch {
        # Fallback: just try taskkill
        & taskkill /F /T /PID $ParentPid 2>&1 | Out-Null
    }
    $ErrorActionPreference = "Stop"
}

# ---------------------------------------------------------------------------
# Helper: find and kill ALL processes holding a specific port
# ---------------------------------------------------------------------------
function Kill-PortProcesses {
    param([int]$Port)

    $ErrorActionPreference = "Continue"
    # Use Get-NetTCPConnection for reliable PID lookup (available on Win8+)
    $pids = @()
    try {
        $conns = Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue
        foreach ($conn in $conns) {
            if ($conn.OwningProcess -and $conn.OwningProcess -ne 0) {
                $pids += $conn.OwningProcess
            }
        }
    } catch {
        # Fallback to netstat
        $pat = ":${Port}\s"
        $lines = netstat -ano | Select-String $pat
        foreach ($line in $lines) {
            $parts = ($line -split '\s+') | Where-Object { $_ }
            $procId = $parts[-1]
            if ($procId -match '^\d+$' -and $procId -ne "0") {
                $pids += [int]$procId
            }
        }
    }
    $ErrorActionPreference = "Stop"

    $pids = $pids | Sort-Object -Unique
    foreach ($procId in $pids) {
        $procName = try { (Get-Process -Id $procId -ErrorAction SilentlyContinue).ProcessName } catch { "unknown" }
        Write-Host "  Killing port $Port holder PID=$procId ($procName)" -ForegroundColor Yellow
        # Use taskkill /T first — most reliable for killing process trees on Windows
        & taskkill /F /T /PID $procId 2>&1 | Out-Null
        Kill-ProcessTree -ParentPid $procId
    }
    return $pids.Count
}

# ---------------------------------------------------------------------------
# Stop logic
# ---------------------------------------------------------------------------
function Stop-Services {
    $savedEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    Write-Host "=== Stopping Multi-Agent Studio ===" -ForegroundColor Cyan

    # Step 1: Kill via PID files — kill entire process tree (cmd.exe + children)
    $pidFiles = @("backend.pid", "frontend.pid")
    foreach ($pf in $pidFiles) {
        $pidPath = Join-Path $PidDir $pf
        if (Test-Path $pidPath) {
            $procId = [int](Get-Content $pidPath -Raw).Trim()
            if ($procId -gt 0) {
                $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
                if ($proc) {
                    Write-Host "  Stopping $($pf.Replace('.pid','')) (cmd.exe PID=$procId)" -ForegroundColor Yellow
                    # Use taskkill /T to kill the entire process tree
                    & taskkill /F /T /PID $procId 2>&1 | Out-Null
                    # Also use our tree killer as backup
                    Kill-ProcessTree -ParentPid $procId
                }
            }
            Remove-Item $pidPath -Force -ErrorAction SilentlyContinue
        }
    }

    Start-Sleep -Milliseconds 500

    # Step 2: Kill any remaining processes on our ports
    foreach ($port in $Ports) {
        $count = Kill-PortProcesses -Port $port
        if ($count -eq 0) {
            Write-Host "  Port ${port}: no processes found" -ForegroundColor Gray
        }
    }

    Start-Sleep -Milliseconds 500

    # Step 3: Kill any remaining python/node processes that match our project
    # This catches orphaned uvicorn workers (multiprocessing.spawn with parent_pid)
    $pyProcs = Get-Process python* -ErrorAction SilentlyContinue |
        Where-Object {
            try {
                $cmdLine = (Get-CimInstance Win32_Process -Filter "ProcessId=$($_.Id)" -ErrorAction SilentlyContinue).CommandLine
                $cmdLine -and ($cmdLine -match "app\.main" -or $cmdLine -match "uvicorn" -or $cmdLine -match "watchfiles" -or $cmdLine -match "orchestrator" -or $cmdLine -match "multiprocessing\.spawn.*parent_pid")
            } catch { $false }
        }

    foreach ($p in $pyProcs) {
        Write-Host "  Killing python PID=$($p.Id)" -ForegroundColor Yellow
        & taskkill /F /T /PID $p.Id 2>&1 | Out-Null
        Kill-ProcessTree -ParentPid $p.Id
    }

    $nodeProcs = Get-Process node* -ErrorAction SilentlyContinue |
        Where-Object {
            try {
                $cmdLine = (Get-CimInstance Win32_Process -Filter "ProcessId=$($_.Id)" -ErrorAction SilentlyContinue).CommandLine
                $cmdLine -and ($cmdLine -match "next" -or $cmdLine -match "apps\\web")
            } catch { $false }
        }

    foreach ($p in $nodeProcs) {
        Write-Host "  Killing node PID=$($p.Id)" -ForegroundColor Yellow
        & taskkill /F /T /PID $p.Id 2>&1 | Out-Null
        Kill-ProcessTree -ParentPid $p.Id
    }

    Start-Sleep -Seconds 2

    # Step 4: Final verification — force kill anything still on ports, with retries
    $maxRetries = 3
    for ($attempt = 1; $attempt -le $maxRetries; $attempt++) {
        $allFree = $true
        foreach ($port in $Ports) {
            $pidsOnPort = @()
            try {
                $conns = Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue
                foreach ($conn in $conns) {
                    if ($conn.OwningProcess -and $conn.OwningProcess -ne 0) {
                        $pidsOnPort += $conn.OwningProcess
                    }
                }
            } catch {
                $pat = ":${port}\s"
                $lines = netstat -ano | Select-String $pat
                foreach ($line in $lines) {
                    $parts = ($line -split '\s+') | Where-Object { $_ }
                    $procId = $parts[-1]
                    if ($procId -match '^\d+$' -and $procId -ne "0") {
                        $pidsOnPort += [int]$procId
                    }
                }
            }
            $pidsOnPort = $pidsOnPort | Sort-Object -Unique
            if ($pidsOnPort.Count -gt 0) {
                $allFree = $false
                foreach ($rpid in $pidsOnPort) {
                    $procName = try { (Get-Process -Id $rpid -ErrorAction SilentlyContinue).ProcessName } catch { "unknown" }
                    Write-Host "  [Attempt $attempt] Force killing PID=$rpid ($procName) on port $port" -ForegroundColor Red
                    # Use /T to kill the entire process tree, /F for force
                    & taskkill /F /T /PID $rpid 2>&1 | Out-Null
                    # Also kill via Stop-Process as backup
                    Stop-Process -Id $rpid -Force -ErrorAction SilentlyContinue
                }
            }
        }
        if ($allFree) { break }
        if ($attempt -lt $maxRetries) {
            Write-Host "  Waiting 2 seconds before retry..." -ForegroundColor Gray
            Start-Sleep -Seconds 2
        }
    }

    # Final status report
    $clean = $true
    foreach ($p in $Ports) {
        $stillOccupied = $false
        try {
            $conns = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue
            if ($conns) { $stillOccupied = $true }
        } catch {
            $pat = ":${p}\s"
            $check = netstat -ano | Select-String $pat
            if ($check) { $stillOccupied = $true }
        }
        if ($stillOccupied) {
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
        Write-Host "Some ports still occupied. Run '.\scripts\start.ps1 stop' again." -ForegroundColor Yellow
    }
    $ErrorActionPreference = $savedEAP
}

# ---------------------------------------------------------------------------
# Start logic
# ---------------------------------------------------------------------------
function Start-Services {
    if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
    if (-not (Test-Path $PidDir)) { New-Item -ItemType Directory -Path $PidDir -Force | Out-Null }

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
    Write-Host "  Cleaning up old processes..." -ForegroundColor Gray
    $savedEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    foreach ($port in $Ports) {
        Kill-PortProcesses -Port $port
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
    Write-Host "    cmd.exe PID=$($backendProc.Id)" -ForegroundColor DarkGray
    # Save cmd.exe PID for clean shutdown
    Set-Content -Path (Join-Path $PidDir "backend.pid") -Value $backendProc.Id -Force

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
    Write-Host "    cmd.exe PID=$($frontendProc.Id)" -ForegroundColor DarkGray
    # Save cmd.exe PID for clean shutdown
    Set-Content -Path (Join-Path $PidDir "frontend.pid") -Value $frontendProc.Id -Force

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
