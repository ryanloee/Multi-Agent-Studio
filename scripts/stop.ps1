# Multi-Agent Studio - Stop All Services
# Kills processes on ports 8000 (backend) and 3000 (frontend)
# Usage: .\scripts\stop.ps1

Write-Host "=== Stopping Multi-Agent Studio ===" -ForegroundColor Cyan

$ports = @(8000, 3000)

# Collect all PIDs occupying our ports
$targetPids = @()
foreach ($p in $ports) {
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

# Kill orphan python processes running our app (python3.12, python, etc.)
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

# Also kill any straggler python3.12 processes on our ports
$portPids = @()
foreach ($p in $ports) {
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
foreach ($p in $ports) {
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
