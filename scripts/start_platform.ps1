<#
.SYNOPSIS
    Starts the full algo-trading platform stack on Windows: Docker-backed
    MySQL/Redis, Django/Daphne, the default AND priority Celery workers,
    Celery Beat, the live Angel One tick feed, and the React/Vite frontend
    -- each as its own tracked, logged background process.

.DESCRIPTION
    Every process this script starts gets its own log file (logs\<name>.log
    + logs\<name>.err.log, rotated once past ~20MB so a long-running feed
    process can never silently grow into a multi-hundred-MB file the way
    this platform's own run_live_feed.log has in the past) and its own PID
    file (logs\pids\<name>.pid). Re-running this script skips any service
    whose recorded PID is still alive instead of starting a second copy.

    Never touches a process it did not itself start and record a PID for
    -- see stop_platform.ps1 for the corresponding safe-stop counterpart.

    Requires: Docker Desktop running, a backend virtual environment already
    created (backend\.venv or backend\venv) with requirements.txt
    installed, backend\.env and docker\.env already filled in from their
    .example templates, and Node.js/npm on PATH for the frontend.

.PARAMETER SkipDocker
    Skip the Docker/MySQL/Redis startup step (use when they are already
    running some other way).

.PARAMETER SkipFrontend
    Skip starting the React/Vite dev server (backend-only startup).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\start_platform.ps1
#>

[CmdletBinding()]
param(
    [switch]$SkipDocker,
    [switch]$SkipFrontend
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$BackendDir = Join-Path $RepoRoot "backend"
$FrontendDir = Join-Path $RepoRoot "frontend"
$DockerDir = Join-Path $RepoRoot "docker"
$LogDir = Join-Path $RepoRoot "logs"
$PidDir = Join-Path $LogDir "pids"
$MaxLogBytes = 20MB

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
New-Item -ItemType Directory -Force -Path $PidDir | Out-Null

function Write-Step {
    param([string]$Message)
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Backup-LargeLog {
    param([string]$Path)
    if (Test-Path $Path) {
        $item = Get-Item $Path
        if ($item.Length -gt $MaxLogBytes) {
            $backup = "$Path.1"
            if (Test-Path $backup) { Remove-Item $backup -Force }
            Move-Item $Path $backup -Force
        }
    }
}

function Test-TrackedProcessAlive {
    param([string]$Name)
    $pidFile = Join-Path $PidDir "$Name.pid"
    if (-not (Test-Path $pidFile)) { return $false }
    $recordedId = Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $recordedId) { return $false }
    $proc = Get-Process -Id $recordedId -ErrorAction SilentlyContinue
    return $null -ne $proc
}

function Format-ProcessArgument {
    # Start-Process -ArgumentList joins array elements into the child's
    # command line with a plain space -- it does NOT quote an element
    # that itself contains a space. This repo's own path
    # ("...\claude ai algo trading platform\...") has spaces, so an
    # unquoted manage.py path silently splits into several argv entries
    # (observed for real: python reported "can't open file
    # 'E:\algo_trading\claude'" -- truncated at the first space).  Every
    # argument containing whitespace must be individually quoted before
    # being handed to Start-Process.
    param([string]$Value)
    if ($Value -match '\s' -and $Value -notmatch '^".*"$') {
        return '"' + $Value + '"'
    }
    return $Value
}

function Start-TrackedProcess {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$ArgumentList,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory
    )

    if (Test-TrackedProcessAlive $Name) {
        $existingId = Get-Content (Join-Path $PidDir "$Name.pid")
        Write-Host "  $Name already running (PID $existingId) -- skipping." -ForegroundColor Yellow
        return
    }

    $logPath = Join-Path $LogDir "$Name.log"
    $errLogPath = Join-Path $LogDir "$Name.err.log"
    Backup-LargeLog $logPath
    Backup-LargeLog $errLogPath

    $quotedArgs = @($ArgumentList | ForEach-Object { Format-ProcessArgument $_ })

    $proc = Start-Process -FilePath $FilePath -ArgumentList $quotedArgs `
        -WorkingDirectory $WorkingDirectory `
        -RedirectStandardOutput $logPath -RedirectStandardError $errLogPath `
        -WindowStyle Hidden -PassThru

    Start-Sleep -Milliseconds 500
    if ($proc.HasExited) {
        Write-Host "  $Name (PID $($proc.Id)) exited immediately (exit code $($proc.ExitCode)) -- check logs\$Name.err.log" -ForegroundColor Red
        return
    }

    Set-Content -Path (Join-Path $PidDir "$Name.pid") -Value $proc.Id
    Write-Host "  Started $Name (PID $($proc.Id)) -- log: logs\$Name.log" -ForegroundColor Green
}

# ---------------------------------------------------------------------
# 1. Docker + MySQL/Redis
# ---------------------------------------------------------------------
if (-not $SkipDocker) {
    Write-Step "Checking Docker..."
    try {
        docker version --format "{{.Server.Version}}" | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "docker version exited with code $LASTEXITCODE" }
    } catch {
        throw "Docker does not appear to be installed or running. Start Docker Desktop and re-run this script (or pass -SkipDocker if MySQL/Redis are already running some other way). Detail: $_"
    }
    Write-Host "  Docker OK." -ForegroundColor Green

    $dockerEnvPath = Join-Path $DockerDir ".env"
    if (-not (Test-Path $dockerEnvPath)) {
        throw "docker\.env not found. Copy docker\.env.example to docker\.env and fill in real MySQL credentials first -- see that file's own comments."
    }

    Write-Step "Starting MySQL and Redis (docker compose)..."
    Push-Location $DockerDir
    try {
        docker compose up -d
        if ($LASTEXITCODE -ne 0) { throw "docker compose up failed with exit code $LASTEXITCODE." }
    } finally {
        Pop-Location
    }
    Write-Host "  MySQL and Redis are up." -ForegroundColor Green
}

# ---------------------------------------------------------------------
# 2. Backend virtual environment
# ---------------------------------------------------------------------
Write-Step "Locating the backend virtual environment..."
$venvCandidates = @(
    (Join-Path $BackendDir ".venv\Scripts\python.exe"),
    (Join-Path $BackendDir "venv\Scripts\python.exe")
)
$PythonExe = $null
foreach ($candidate in $venvCandidates) {
    if (Test-Path $candidate) { $PythonExe = $candidate; break }
}
if (-not $PythonExe) {
    throw "No backend virtual environment found (checked backend\.venv and backend\venv). Create one first: cd backend; python -m venv .venv; .venv\Scripts\pip install -r requirements.txt"
}
Write-Host "  Using $PythonExe" -ForegroundColor Green

$ManagePy = Join-Path $BackendDir "manage.py"

# ---------------------------------------------------------------------
# 3. Django checks + migrations (pending only -- `migrate` never
#    generates new migration files, only applies ones already on disk)
# ---------------------------------------------------------------------
Write-Step "Running python manage.py check..."
& $PythonExe $ManagePy check
if ($LASTEXITCODE -ne 0) { throw "manage.py check failed -- fix the reported issues before starting services." }
Write-Host "  check passed." -ForegroundColor Green

Write-Step "Applying pending migrations..."
& $PythonExe $ManagePy migrate --noinput
if ($LASTEXITCODE -ne 0) { throw "manage.py migrate failed." }
Write-Host "  migrations applied." -ForegroundColor Green

# ---------------------------------------------------------------------
# 4. Application processes
# ---------------------------------------------------------------------
Write-Step "Starting Django/Daphne (runserver)..."
Start-TrackedProcess -Name "django" -FilePath $PythonExe `
    -ArgumentList @($ManagePy, "runserver", "0.0.0.0:8000") `
    -WorkingDirectory $BackendDir

Write-Step "Starting the default Celery worker (-Q celery)..."
Start-TrackedProcess -Name "celery_worker_default" -FilePath $PythonExe `
    -ArgumentList @("-m", "celery", "-A", "config", "worker", "-l", "info", "--pool=solo", "-Q", "celery") `
    -WorkingDirectory $BackendDir

Write-Step "Starting the priority Celery worker (-Q priority)..."
Start-TrackedProcess -Name "celery_worker_priority" -FilePath $PythonExe `
    -ArgumentList @("-m", "celery", "-A", "config", "worker", "-l", "info", "--pool=solo", "-Q", "priority") `
    -WorkingDirectory $BackendDir

Write-Step "Starting Celery Beat..."
Start-TrackedProcess -Name "celery_beat" -FilePath $PythonExe `
    -ArgumentList @("-m", "celery", "-A", "config", "beat", "-l", "info") `
    -WorkingDirectory $BackendDir

Write-Step "Starting the live Angel One tick feed (run_live_feed)..."
Start-TrackedProcess -Name "run_live_feed" -FilePath $PythonExe `
    -ArgumentList @($ManagePy, "run_live_feed") `
    -WorkingDirectory $BackendDir

if (-not $SkipFrontend) {
    Write-Step "Starting the React/Vite frontend..."
    $npmCmd = Get-Command npm -ErrorAction SilentlyContinue
    if (-not $npmCmd) {
        Write-Warning "npm was not found on PATH -- skipping the frontend. Install Node.js, or pass -SkipFrontend to silence this warning."
    } else {
        # npm on Windows resolves to npm.cmd (a batch file) / a shebang
        # script, neither of which Start-Process -FilePath can execute
        # directly (it needs an actual PE binary) -- routed through
        # cmd.exe /c the same way any other batch-file launch on Windows
        # must be. This means the PID this script records is cmd.exe's
        # own, with node/vite running as its child processes -- see
        # stop_platform.ps1's own comment on why it tree-kills via
        # `taskkill /T` rather than Stop-Process for exactly this reason.
        Start-TrackedProcess -Name "frontend" -FilePath "$env:WINDIR\System32\cmd.exe" `
            -ArgumentList @("/c", "npm run dev") `
            -WorkingDirectory $FrontendDir
    }
}

# ---------------------------------------------------------------------
# 5. Summary
# ---------------------------------------------------------------------
Write-Host ""
Write-Host "===================================================================" -ForegroundColor Cyan
Write-Host " Running processes (PID files under logs\pids\, logs under logs\):" -ForegroundColor Cyan
Get-ChildItem $PidDir -Filter "*.pid" | Sort-Object Name | ForEach-Object {
    $name = $_.BaseName
    $recordedId = Get-Content $_.FullName
    "   {0,-24} PID {1,-8} log: logs\{0}.log" -f $name, $recordedId | Write-Host
}
Write-Host ""
Write-Host " Frontend:     http://localhost:3000" -ForegroundColor Green
Write-Host " Backend API:  http://127.0.0.1:8000/api/" -ForegroundColor Green
Write-Host " Health check: http://127.0.0.1:8000/api/monitoring/health/" -ForegroundColor Green
Write-Host "===================================================================" -ForegroundColor Cyan
Write-Host " Stop everything this script started with: scripts\stop_platform.ps1" -ForegroundColor Yellow
