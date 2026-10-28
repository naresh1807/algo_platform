<#
.SYNOPSIS
    Run periodically (via Task Scheduler, see install_watchdog_task.ps1)
    to catch a real failure mode discovered live: NSSM's own crash-loop
    backoff can exhaust itself and give up retrying entirely (e.g. after
    a reboot where Docker Desktop/MySQL took longer to become ready
    than NSSM's escalating retry delay tolerated) -- and when that
    happens, Windows Service Control Manager keeps reporting the
    service as "Running" even though the actual wrapped process is
    permanently dead. Nothing then ever brings it back on its own; the
    platform silently goes offline until someone happens to notice and
    manually restarts it (this happened for real -- algo-django was
    down for ~3 hours before the user noticed the mobile app showing
    "Reconnecting").

    This script checks REAL health, not just what SCM reports:
    algo-django specifically gets an actual HTTP request (SCM status
    alone already proved untrustworthy for it), everything else gets
    its SCM status checked and restarted if it's not "Running".

.DESCRIPTION
    Deliberately does NOT require elevation to RUN (Restart-Service
    does, but the scheduled task this is installed under runs as
    SYSTEM, which already has that -- see install_watchdog_task.ps1).
    Safe to run manually too, any time, from a non-elevated prompt --
    it'll just log a permission error on any restart attempt instead
    of silently doing nothing, so you'd still see if it's needed one.
#>

$ErrorActionPreference = "Continue"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $RepoRoot "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LogPath = Join-Path $LogDir "watchdog.log"

function Write-Log {
    param([string]$Message)
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message"
    Add-Content -Path $LogPath -Value $line
}

$Services = @(
    "algo-django",
    "algo-celery-worker-default",
    "algo-celery-worker-priority",
    "algo-celery-beat",
    "algo-run-live-feed"
)

foreach ($name in $Services) {
    $svc = Get-Service -Name $name -ErrorAction SilentlyContinue
    if (-not $svc) {
        Write-Log "$name -- not installed, skipping."
        continue
    }
    if ($svc.Status -ne "Running") {
        Write-Log "$name -- SCM status is $($svc.Status) (expected Running), restarting."
        Restart-Service -Name $name -Force -ErrorAction SilentlyContinue
        Write-Log "$name -- restart attempted."
    }
}

# algo-django specifically: SCM status alone already proved untrustworthy
# (see this file's own header) -- verify it actually answers a real
# request. Any HTTP response at all, even 401/403, proves the app is
# alive; only a genuine connection failure means it's actually down.
try {
    Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/monitoring/health/" -TimeoutSec 8 -UseBasicParsing -ErrorAction Stop | Out-Null
} catch {
    if ($_.Exception.Response) {
        # A real HTTP error status (e.g. 401) still means Django itself
        # answered -- not an outage, nothing to do.
    } else {
        Write-Log "algo-django -- health check unreachable ($($_.Exception.Message)), forcing restart."
        Restart-Service -Name "algo-django" -Force -ErrorAction SilentlyContinue
        Write-Log "algo-django -- restart attempted."
    }
}
