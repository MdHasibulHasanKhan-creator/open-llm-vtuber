<#
.SYNOPSIS
    Start Open VRM VTuber (backend + frontend) for development.

.DESCRIPTION
    Boots the FastAPI backend (http://127.0.0.1:8000) and the Vite dev server
    (http://localhost:5173) in one go. On the first run it creates the Python
    virtual environment, installs both dependency sets, and copies the example
    config files. Everything after that is a plain start.

.PARAMETER BackendOnly
    Only run the backend.

.PARAMETER FrontendOnly
    Only run the frontend (assumes the backend is already up).

.PARAMETER SkipInstall
    Do not create the venv / install anything, just run.

.PARAMETER Port
    Backend port (default 8000).

.PARAMETER NoReload
    Disable uvicorn's auto-reload.

.EXAMPLE
    .\scripts\dev.ps1
    .\scripts\dev.ps1 -BackendOnly -Port 8765
#>
[CmdletBinding()]
param(
    [switch]$BackendOnly,
    [switch]$FrontendOnly,
    [switch]$SkipInstall,
    [int]$Port = 0,
    [switch]$NoReload
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$Root = Split-Path -Parent $PSScriptRoot
$BackendDir = Join-Path $Root 'backend'
$FrontendDir = Join-Path $Root 'frontend'
$VenvPython = Join-Path $BackendDir '.venv\Scripts\python.exe'

# StrictMode would throw on an undefined variable in the cleanup path.
$backend = $null

function Write-Step([string]$Message) {
    Write-Host ''
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Write-Warn([string]$Message) {
    Write-Host "    $Message" -ForegroundColor Yellow
}

function Test-Command([string]$Name) {
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

# --------------------------------------------------------------------------- #
#  Preconditions
# --------------------------------------------------------------------------- #
if (-not $FrontendOnly -and -not (Test-Command 'python')) {
    throw "python was not found on PATH. Install Python 3.11+ and try again."
}
if (-not $BackendOnly -and -not (Test-Command 'npm')) {
    throw "npm was not found on PATH. Install Node.js 20.19+ and try again."
}

# --------------------------------------------------------------------------- #
#  Backend setup
# --------------------------------------------------------------------------- #
if (-not $SkipInstall -and -not $FrontendOnly) {
    if (-not (Test-Path $VenvPython)) {
        Write-Step 'Creating the Python virtual environment (backend/.venv)'
        python -m venv (Join-Path $BackendDir '.venv')
    }

    Write-Step 'Installing backend dependencies'
    & $VenvPython -m pip install --upgrade pip --quiet
    & $VenvPython -m pip install -r (Join-Path $BackendDir 'requirements.txt') --quiet

    $devReq = Join-Path $BackendDir 'requirements-dev.txt'
    if (Test-Path $devReq) {
        # Nice to have: pytest + ruff. Never fatal.
        & $VenvPython -m pip install -r $devReq --quiet
    }

    $localReq = Join-Path $BackendDir 'requirements-local.txt'
    if (Test-Path $localReq) {
        Write-Host ''
        Write-Host '    Optional offline speech recognition (faster-whisper):' -ForegroundColor DarkGray
        Write-Host '      pip install -r backend\requirements-local.txt' -ForegroundColor DarkGray
    }

    $backendEnv = Join-Path $BackendDir '.env'
    if (-not (Test-Path $backendEnv)) {
        Copy-Item (Join-Path $BackendDir '.env.example') $backendEnv
        Write-Warn 'Created backend\.env from .env.example - edit it to point at your LLM.'
    }
}

# --------------------------------------------------------------------------- #
#  Frontend setup
# --------------------------------------------------------------------------- #
if (-not $SkipInstall -and -not $BackendOnly) {
    if (-not (Test-Path (Join-Path $FrontendDir 'node_modules'))) {
        Write-Step 'Installing frontend dependencies (npm install)'
        Push-Location $FrontendDir
        try { npm install } finally { Pop-Location }
    }

    $frontendEnv = Join-Path $FrontendDir '.env.local'
    if (-not (Test-Path $frontendEnv) -and (Test-Path (Join-Path $FrontendDir '.env.example'))) {
        # Optional file: the defaults already work for the standard two-terminal setup.
        Copy-Item (Join-Path $FrontendDir '.env.example') $frontendEnv
        Write-Warn 'Created frontend\.env.local from .env.example (all values optional).'
    }
}

# --------------------------------------------------------------------------- #
#  Run
# --------------------------------------------------------------------------- #
$backendArgs = @()
if ($Port -gt 0) { $backendArgs += @('--port', $Port) }
if (-not $NoReload) { $backendArgs += '--reload' }

if (-not $FrontendOnly) {
    Write-Step "Starting the backend on http://127.0.0.1:$(if ($Port -gt 0) { $Port } else { 8000 })"
    if ($BackendOnly) {
        Push-Location $BackendDir
        try { & $VenvPython 'main.py' @backendArgs } finally { Pop-Location }
        exit $LASTEXITCODE
    }
    $backend = Start-Process -PassThru -FilePath $VenvPython `
        -ArgumentList (@('main.py') + $backendArgs) -WorkingDirectory $BackendDir
    Write-Host "    backend pid $($backend.Id)"
}

if (-not $BackendOnly) {
    Write-Step 'Starting the frontend on http://localhost:5173'
    try {
        Push-Location $FrontendDir
        npm run dev
    }
    finally {
        Pop-Location
        if ($backend -and -not $backend.HasExited) {
            Write-Step 'Stopping the backend'
            Stop-Process -Id $backend.Id -Force -ErrorAction SilentlyContinue
        }
    }
}
