<#
.SYNOPSIS
    Build MAS Studio standalone Windows distribution.

.DESCRIPTION
    1. Builds the Next.js frontend with static export (output -> apps/web/out/)
    2. Installs Python dependencies (orchestrator + agent)
    3. Runs PyInstaller to produce a self-contained dist/MAS-Studio/ directory
    4. Zips the result into dist/MAS-Studio-v<Version>-win64.zip

.PARAMETER Version
    Version string embedded in the archive name.  Defaults to "0.1.0".

.EXAMPLE
    .\scripts\build.ps1 -Version 1.0.0
#>
param(
    [string]$Version = "0.1.0"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")

Write-Host "=== Building MAS Studio v$Version ===" -ForegroundColor Cyan
Write-Host "Project root: $ProjectRoot`n"

# ------------------------------------------------------------------
# 1. Build frontend
# ------------------------------------------------------------------
Write-Host "[1/4] Building frontend (static export)..." -ForegroundColor Yellow

Push-Location "$ProjectRoot\apps\web"
try {
    pnpm install --frozen-lockfile
    pnpm build
    if (-not (Test-Path ".\out\index.html")) {
        Write-Error "Frontend build did not produce apps/web/out/index.html.  Ensure output:'export' is set in next.config.js."
        exit 1
    }
    Write-Host "       Frontend output: $(Resolve-Path '.\out')`n"
} finally {
    Pop-Location
}

# ------------------------------------------------------------------
# 2. Install Python dependencies
# ------------------------------------------------------------------
Write-Host "[2/4] Installing Python dependencies..." -ForegroundColor Yellow

Push-Location "$ProjectRoot\apps\orchestrator"
try {
    pip install --quiet pyinstaller
    poetry install --no-interaction
} finally {
    Pop-Location
}

# ------------------------------------------------------------------
# 3. PyInstaller
# ------------------------------------------------------------------
Write-Host "[3/4] Building executable with PyInstaller..." -ForegroundColor Yellow

$webOut   = "$ProjectRoot\apps\web\out"
$agentPkg = "$ProjectRoot\apps\agent\mas_agent"

# PyInstaller must run from the repo root so relative --add-data paths resolve.
Push-Location $ProjectRoot
try {
    pyinstaller --noconfirm --onedir `
        --name "MAS-Studio" `
        --add-data "$webOut;static" `
        --add-data "$agentPkg;mas_agent" `
        --hidden-import uvicorn.logging `
        --hidden-import uvicorn.loops.auto `
        --hidden-import uvicorn.protocols.http.auto `
        --hidden-import uvicorn.protocols.websockets.auto `
        --hidden-import uvicorn.lifespan.on `
        --hidden-import app.main `
        --hidden-import app.config `
        --hidden-import app.api.workflows `
        --hidden-import app.api.runs `
        --hidden-import app.api.models `
        --hidden-import app.api.tasks `
        --hidden-import app.api.planner_chat `
        --hidden-import app.api.settings `
        --hidden-import app.core.local_engine `
        --hidden-import app.core.local_sandbox `
        --hidden-import app.core.local_bus `
        --hidden-import app.core.database `
        --hidden-import app.core.task_scheduler `
        --hidden-import app.workflows.compiler `
        --hidden-import app.workflows.plan_parser `
        --hidden-import app.models.db `
        --hidden-import app.models.schemas `
        --hidden-import app.models.task `
        --hidden-import app.sandbox.checkpoint `
        --hidden-import app.sandbox.provision `
        --hidden-import app.ws.hub `
        --hidden-import mas_agent `
        --hidden-import mas_agent.loop `
        --hidden-import mas_agent.providers `
        --hidden-import mas_agent.providers.base `
        --hidden-import mas_agent.providers.anthropic_provider `
        --hidden-import mas_agent.tools `
        --hidden-import mas_agent.tools.glob_tool `
        --hidden-import mas_agent.tools.grep_tool `
        --hidden-import mas_agent.tools.read_tool `
        --hidden-import mas_agent.tools.write_tool `
        --hidden-import mas_agent.tools.edit_tool `
        --hidden-import mas_agent.tools.shell_tool `
        --hidden-import mas_agent.tools.apply_patch_tool `
        --hidden-import mas_agent.tools.output_utils `
        --hidden-import mas_agent.prompts `
        --hidden-import mas_agent.events `
        --hidden-import mas_agent.permission `
        --hidden-import mas_agent.compaction `
        --hidden-import mas_agent.snapshot `
        --hidden-import mas_agent.tool_repair `
        --hidden-import mas_agent.types `
        --hidden-import aiosqlite `
        --collect-all aiosqlite `
        "apps\orchestrator\app\launcher.py"
} finally {
    Pop-Location
}

# ------------------------------------------------------------------
# 4. Package
# ------------------------------------------------------------------
Write-Host "`n[4/4] Packaging..." -ForegroundColor Yellow

$distDir = "$ProjectRoot\dist\MAS-Studio"
if (-not (Test-Path $distDir)) {
    Write-Error "PyInstaller output not found at $distDir"
    exit 1
}

$archive = "$ProjectRoot\dist\MAS-Studio-v$Version-win64.zip"
if (Test-Path $archive) { Remove-Item $archive -Force }
Compress-Archive -Path $distDir -DestinationPath $archive -Force

Write-Host "`n=== Build complete! ===" -ForegroundColor Green
Write-Host "Archive: $archive"
