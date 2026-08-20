<#
.SYNOPSIS
    Stops only the processes started by scripts\start_platform.ps1, using
    the PID files it recorded under logs\pids\.

.DESCRIPTION
    Never touches any process this script did not itself start:
    - Only PIDs found in logs\pids\*.pid are ever considered.
    - Before stopping, each PID's actual command line is checked (via CIM)
      to still look like the service it was recorded as (e.g. contains
      "manage.py" and "runserver" for the django entry) -- if the PID has
      been recycled by an unrelated process since this platform last ran,
      it is left alone and reported, never killed.
    - Never calls a broad "stop everything named python/node/docker"
      command of any kind.
    - Stops via `taskkill /T` (tree-kill), not plain Stop-Process: the
      frontend entry is recorded as cmd.exe's own PID (see
      start_platform.ps1's own comment on why npm must be launched
      through cmd.exe on Windows), with node/vite running as ITS
      children -- Stop-Process on just the parent would leave the vite
      dev server orphaned and still running. Every other tracked
      process is a single process with no children, so tree-killing it
      is equivalent to a plain kill.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\stop_platform.ps1
#>

[CmdletBinding()]
param()

$RepoRoot = Split-Path -Parent $PSScriptRoot
$PidDir = Join-Path $RepoRoot "logs\pids"

# What each tracked process's command line must still contain to be
# considered "still the same service" before this script will stop it.
$ExpectedMarkers = @{
    "django"                   = @("manage.py", "runserver")
    "celery_worker_default"    = @("celery", "worker")
    "celery_worker_priority"   = @("celery", "worker", "priority")
    "celery_beat"              = @("celery", "beat")
    "run_live_feed"            = @("manage.py", "run_live_feed")
    # cmd.exe's own command line ("cmd /c npm run dev"), not vite's --
    # see start_platform.ps1's own comment on why npm is launched
    # through cmd.exe and this script's header comment on why that
    # means tree-killing, not vite's process identity, is what matters
    # here.
    "frontend"                 = @("npm", "run", "dev")
}

if (-not (Test-Path $PidDir)) {
    Write-Host "No logs\pids directory found -- nothing recorded as started by start_platform.ps1." -ForegroundColor Yellow
    exit 0
}

$pidFiles = Get-ChildItem $PidDir -Filter "*.pid" -ErrorAction SilentlyContinue
if (-not $pidFiles) {
    Write-Host "No PID files found under logs\pids -- nothing to stop." -ForegroundColor Yellow
    exit 0
}

foreach ($file in $pidFiles) {
    $name = $file.BaseName
    $recordedId = Get-Content $file.FullName -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $recordedId) {
        Write-Host "  $name -- empty/unreadable PID file, removing it." -ForegroundColor Yellow
        Remove-Item $file.FullName -Force
        continue
    }

    $proc = Get-Process -Id $recordedId -ErrorAction SilentlyContinue
    if (-not $proc) {
        Write-Host "  $name (PID $recordedId) -- not running any more, removing stale PID file." -ForegroundColor DarkGray
        Remove-Item $file.FullName -Force
        continue
    }

    $commandLine = ""
    try {
        $cim = Get-CimInstance Win32_Process -Filter "ProcessId = $recordedId" -ErrorAction Stop
        $commandLine = $cim.CommandLine
    } catch {
        $commandLine = ""
    }

    $markers = $ExpectedMarkers[$name]
    $looksRight = $true
    if ($markers -and $commandLine) {
        foreach ($marker in $markers) {
            if ($commandLine -notlike "*$marker*") { $looksRight = $false; break }
        }
    } elseif ($markers -and -not $commandLine) {
        # Could not read the command line (e.g. insufficient privilege) --
        # do not guess; refuse to stop rather than risk killing the wrong
        # process.
        $looksRight = $false
    }

    if (-not $looksRight) {
        Write-Host "  $name (PID $recordedId) -- command line no longer matches what this script started; leaving it alone. Investigate manually if needed." -ForegroundColor Red
        continue
    }

    # /T kills the whole process tree (child processes too -- see this
    # script's header comment on why that matters for the frontend
    # entry specifically), /F forces it, matching Stop-Process -Force's
    # own semantics for every other single-process entry.
    & taskkill /F /T /PID $recordedId | Out-Null
    Write-Host "  Stopped $name (PID $recordedId)." -ForegroundColor Green
    Remove-Item $file.FullName -Force
}

Write-Host "Done." -ForegroundColor Cyan
