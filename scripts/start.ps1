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
# Helper: find root ancestor of a process (walk up until we hit a system/shell)
# ---------------------------------------------------------------------------
function Find-ServiceRoot {
    param([int]$StartPid)

    $current = $StartPid
    $seen = @{}

    while ($true) {
        if ($seen.ContainsKey($current)) { break }
        $seen[$current] = $true

        try {
            $wmiProc = Get-CimInstance Win32_Process -Filter "ProcessId=$current" -ErrorAction SilentlyContinue
            if (-not $wmiProc) { break }

            $parentPid = $wmiProc.ParentProcessId
            if ($parentPid -le 4 -or $parentPid -eq $current) { break }

            $parent = Get-Process -Id $parentPid -ErrorAction SilentlyContinue
            if (-not $parent) { break }

            # Stop at system processes, shells, terminals, and IDEs
            if ($parent.ProcessName -match '^(services|lsass|csrss|System|smss|wininit|svchost|explorer|WindowsTerminal|conhost|devenv|Code|Cursor)$') { break }

            $current = $parentPid
        } catch {
            break
        }
    }

    return $current
}

# ---------------------------------------------------------------------------
# Helper: kill a PID and ALL its descendant processes recursively
# ---------------------------------------------------------------------------
function Kill-ProcessTree {
    param([int]$ParentPid)

    $ErrorActionPreference = "Continue"
    try {
        # Skip if already dead
        $parent = Get-Process -Id $ParentPid -ErrorAction SilentlyContinue
        if (-not $parent) { return }

        # taskkill /F /T is the fastest way to kill a process tree on Windows
        & taskkill /F /T /PID $ParentPid 2>&1 | Out-Null

        # Brief wait then clean up any survivors with filtered WMI queries
        Start-Sleep -Milliseconds 300
        $queue = @($ParentPid)
        $visited = @{}
        while ($queue.Count -gt 0) {
            $current = $queue[0]
            $queue = $queue[1..($queue.Count - 1)]
            if ($visited.ContainsKey($current)) { continue }
            $visited[$current] = $true
            # Filtered query — only returns children of $current, not all processes
            $kids = Get-CimInstance Win32_Process -Filter "ParentProcessId=$current" -ErrorAction SilentlyContinue
            foreach ($kid in $kids) {
                $proc = Get-Process -Id $kid.ProcessId -ErrorAction SilentlyContinue
                if ($proc) {
                    Write-Host "    Killing PID=$($kid.ProcessId) ($($proc.ProcessName))" -ForegroundColor DarkGray
                    Stop-Process -Id $kid.ProcessId -Force -ErrorAction SilentlyContinue
                }
                $queue += $kid.ProcessId
            }
        }
    } catch {
        & taskkill /F /T /PID $ParentPid 2>&1 | Out-Null
    }
    $ErrorActionPreference = "Stop"
}

# ---------------------------------------------------------------------------
# Helper: find and kill ALL processes holding a specific port
# Kills the ROOT ANCESTOR (e.g. pnpm/cmd.exe) not just the port holder,
# so the parent can't respawn a new child.
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
    if ($pids.Count -eq 0) {
        Write-Host "  Port ${Port}: no processes found" -ForegroundColor Gray
        return 0
    }

    # Find root ancestors for each port-holder so we kill the whole launcher tree
    $rootPids = @()
    foreach ($portPid in $pids) {
        $root = Find-ServiceRoot -StartPid $portPid
        if ($rootPids -notcontains $root) {
            $rootPids += $root
        }
    }

    foreach ($rootPid in $rootPids) {
        $rootProc = Get-Process -Id $rootPid -ErrorAction SilentlyContinue
        $rootName = if ($rootProc) { $rootProc.ProcessName } else { "unknown" }
        $portHolderNames = @()
        foreach ($pp in $pids) {
            $pproc = Get-Process -Id $pp -ErrorAction SilentlyContinue
            if ($pproc) { $portHolderNames += "$($pproc.ProcessName)[$pp]" }
        }
        Write-Host "  Port ${Port}: root=$rootName[$rootPid] (holders: $($portHolderNames -join ', '))" -ForegroundColor Yellow
        # taskkill /T kills the entire process tree from root down
        & taskkill /F /T /PID $rootPid 2>&1 | Out-Null
        # Belt-and-suspenders: also walk the tree manually
        Kill-ProcessTree -ParentPid $rootPid
    }

    return $pids.Count
}

# ---------------------------------------------------------------------------
# Helper: wait until a port is free (no LISTEN state)
# ---------------------------------------------------------------------------
function Wait-PortFree {
    param(
        [int]$Port,
        [int]$TimeoutSeconds = 10
    )
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    while ($sw.ElapsedMilliseconds -lt ($TimeoutSeconds * 1000)) {
        $stillListening = $false
        try {
            $conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
            if ($conns) { $stillListening = $true }
        } catch {
            $pat = ":${Port}\s"
            $check = netstat -ano | Select-String $pat
            if ($check) { $stillListening = $true }
        }
        if (-not $stillListening) {
            Write-Host "  Port ${Port}: free" -ForegroundColor Green
            return $true
        }
        Start-Sleep -Milliseconds 500
    }
    Write-Host "  [WARN] Port ${Port} still occupied after ${TimeoutSeconds}s" -ForegroundColor Red
    return $false
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

    # Step 2: Kill any remaining processes on our ports (via root ancestor)
    foreach ($port in $Ports) {
        Kill-PortProcesses -Port $port
    }

    Start-Sleep -Milliseconds 500

    # Step 3: Kill any remaining python/node processes that match our project
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

    # Step 4: Wait for ports to be free
    Write-Host "  Waiting for ports to be released..." -ForegroundColor Gray
    foreach ($port in $Ports) {
        Wait-PortFree -Port $port -TimeoutSeconds 8
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

    # Kill any existing processes on our ports first — use root-ancestor killing
    Write-Host "  Cleaning up old processes..." -ForegroundColor Gray
    $savedEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    foreach ($port in $Ports) {
        Kill-PortProcesses -Port $port
    }
    $ErrorActionPreference = $savedEAP

    # Wait until ports are actually free before starting new services
    Write-Host "  Waiting for ports to be released..." -ForegroundColor Gray
    foreach ($port in $Ports) {
        Wait-PortFree -Port $port -TimeoutSeconds 10
    }

    Write-Host ""

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
