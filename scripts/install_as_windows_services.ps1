<#
.SYNOPSIS
    Installs the platform's 5 backend processes (Django/Daphne, both
    Celery workers, Celery Beat, the live Angel One feed) as real
    Windows Services via NSSM, so the platform runs continuously --
    survives logoff, auto-starts at boot, auto-restarts on crash --
    instead of needing scripts\start_platform.ps1 run by hand every day.

.DESCRIPTION
    Same 5 processes, same commands, same working directory as
    scripts\start_platform.ps1 -- this does not replace that script (it's
    still useful for local dev without installing services), it wraps
    the same launch commands as proper services instead of tracked
    background processes.

    Requires: NSSM (installed this session via `winget install NSSM.NSSM`),
    a backend virtual environment already created (backend\.venv), and
    MUST be run from an elevated (Administrator) PowerShell -- Windows
    Service creation is not possible otherwise. Right-click PowerShell ->
    "Run as administrator", then:
        cd "E:\algo_trading\claude ai algo trading platform\algo_platform"
        powershell -ExecutionPolicy Bypass -File scripts\install_as_windows_services.ps1

    Each service is set to auto-start at boot and auto-restart on crash
    (5s delay, no restart-count limit -- these are meant to run forever).
    Docker Desktop is NOT wrapped as a service here (Docker Desktop for
    Windows runs in the user's session, not as a system service) -- see
    docs\running_as_a_server.md for how boot-time Docker startup is
    handled instead (Docker Desktop's own "start on login" setting) and
    why these services self-heal via their restart-on-failure setting
    even if they start running before Docker/MySQL/Redis are ready.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install_as_windows_services.ps1
#>

[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$currentPrincipal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $currentPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "This script must be run from an elevated (Administrator) PowerShell. Right-click PowerShell -> 'Run as administrator', then re-run this script."
}

$RepoRoot = Split-Path -Parent $PSScriptRoot
$BackendDir = Join-Path $RepoRoot "backend"
$LogDir = Join-Path $RepoRoot "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Find-Nssm {
    $onPath = Get-Command nssm -ErrorAction SilentlyContinue
    if ($onPath) { return $onPath.Source }
    $candidates = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\NSSM.NSSM_*\*\win64\nssm.exe" -ErrorAction SilentlyContinue
    if ($candidates) { return $candidates[0].FullName }
    throw "nssm.exe not found. Install it first: winget install --id NSSM.NSSM -e"
}
$Nssm = Find-Nssm
Write-Host "Using nssm: $Nssm" -ForegroundColor Green

$venvCandidates = @(
    (Join-Path $BackendDir ".venv\Scripts\python.exe"),
    (Join-Path $BackendDir "venv\Scripts\python.exe")
)
$PythonExe = $null
foreach ($candidate in $venvCandidates) {
    if (Test-Path $candidate) { $PythonExe = $candidate; break }
}
if (-not $PythonExe) {
    throw "No backend virtual environment found (checked backend\.venv and backend\venv)."
}
Write-Host "Using python: $PythonExe" -ForegroundColor Green

# Relative "manage.py", not a full path -- AppDirectory (set below) is
# $BackendDir, so this resolves correctly without needing to quote a
# path that contains spaces ("...\claude ai algo trading platform\...").
# None of these tokens contain spaces, so a plain join is safe below.
$Services = [ordered]@{
    "algo-django"                 = @("manage.py", "runserver", "0.0.0.0:8000")
    "algo-celery-worker-default"  = @("-m", "celery", "-A", "config", "worker", "-l", "info", "--pool=solo", "-Q", "celery")
    "algo-celery-worker-priority" = @("-m", "celery", "-A", "config", "worker", "-l", "info", "--pool=solo", "-Q", "priority")
    "algo-celery-beat"            = @("-m", "celery", "-A", "config", "beat", "-l", "info")
    "algo-run-live-feed"          = @("manage.py", "run_live_feed")
}

foreach ($name in $Services.Keys) {
    # Not named $args -- that's PowerShell's reserved automatic variable
    # for a function/script's unbound extra arguments; shadowing it here
    # happened to work but is fragile.
    $svcArgs = $Services[$name]

    # Using Get-Service/Stop-Service here rather than `nssm status`/`nssm
    # stop` -- both are real cmdlets, not native .exe calls, so they
    # can't hit the Windows PowerShell 5.1 quirk where redirecting a
    # native command's stderr (even to $null) gets converted into a
    # terminating error under $ErrorActionPreference = "Stop", which is
    # exactly what happened here on a first run: nssm.exe's normal
    # "service doesn't exist yet" message on stderr killed the whole
    # script despite the `2>$null`.
    $existingService = Get-Service -Name $name -ErrorAction SilentlyContinue
    if ($existingService) {
        Write-Host "  $name already installed (status: $($existingService.Status)) -- removing first so this run is idempotent." -ForegroundColor Yellow
        if ($existingService.Status -ne "Stopped") {
            Stop-Service -Name $name -ErrorAction SilentlyContinue
        }
        & $Nssm remove $name confirm | Out-Null
    }

    Write-Host "==> Installing service: $name" -ForegroundColor Cyan
    # Real bug hit on first run: passing the whole pre-quoted argument
    # string as install's trailing positional arg made nssm store it as
    # one already-quoted blob, which got quoted AGAIN when nssm actually
    # launched python.exe -- python then received one literal argument
    # containing quote characters instead of separate arguments, failed
    # immediately, and Start-Service below errored. `nssm set
    # AppParameters` as its own separate call is NSSM's documented
    # reliable way to set a multi-token argument list.
    & $Nssm install $name $PythonExe
    & $Nssm set $name AppParameters ($svcArgs -join ' ')
    & $Nssm set $name AppDirectory $BackendDir
    & $Nssm set $name AppStdout (Join-Path $LogDir "$name.service.log")
    & $Nssm set $name AppStderr (Join-Path $LogDir "$name.service.err.log")
    & $Nssm set $name AppStdoutCreationDisposition 4   # append, don't truncate on each restart
    & $Nssm set $name AppStderrCreationDisposition 4
    & $Nssm set $name AppRotateFiles 1
    & $Nssm set $name AppRotateBytes 20971520           # 20MB, matches start_platform.ps1's own rotation size
    & $Nssm set $name Start SERVICE_AUTO_START
    # Runs forever, self-healing: if MySQL/Redis aren't up yet (Docker
    # Desktop still starting after a reboot), this process will fail to
    # connect, exit, and NSSM restarts it every 5s until it succeeds --
    # no explicit service dependency on Docker needed.
    & $Nssm set $name AppExit Default Restart
    & $Nssm set $name AppRestartDelay 5000
    & $Nssm set $name DisplayName "Algo Trading Platform - $name"
    & $Nssm set $name Description "Auto-restarting background service -- part of the algo trading platform. Installed by scripts\install_as_windows_services.ps1."

    Start-Service -Name $name
    Start-Sleep -Milliseconds 500
    $svc = Get-Service -Name $name
    Write-Host "  $name -> $($svc.Status)" -ForegroundColor Green
}

Write-Host ""
Write-Host "===================================================================" -ForegroundColor Cyan
Write-Host " Installed as Windows Services (Automatic startup, auto-restart):" -ForegroundColor Cyan
foreach ($name in $Services.Keys) { Write-Host "   $name" }
Write-Host ""
Write-Host " These now start automatically at boot and keep running with nobody" -ForegroundColor Cyan
Write-Host " logged in. Manage them via services.msc, or:" -ForegroundColor Cyan
Write-Host "   nssm status <name>  |  nssm stop <name>  |  nssm start <name>" -ForegroundColor Yellow
Write-Host " Logs: logs\<name>.service.log / .err.log" -ForegroundColor Yellow
Write-Host " To fully remove: scripts\uninstall_windows_services.ps1" -ForegroundColor Yellow
Write-Host "===================================================================" -ForegroundColor Cyan
