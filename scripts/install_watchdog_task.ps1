<#
.SYNOPSIS
    Registers watchdog_services.ps1 as a Windows Scheduled Task running
    every 3 minutes as SYSTEM (no login required -- unlike Docker
    Desktop) so a real, discovered failure mode (see that script's own
    header comment) can't strand the platform offline for hours again.

.DESCRIPTION
    Requires an elevated PowerShell -- Task Scheduler registration under
    the SYSTEM account needs it, same as install_as_windows_services.ps1.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install_watchdog_task.ps1
#>

[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$currentPrincipal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $currentPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "This script must be run from an elevated (Administrator) PowerShell."
}

$TaskName = "AlgoPlatformWatchdog"
$ScriptPath = Join-Path $PSScriptRoot "watchdog_services.ps1"

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Write-Host "Removing existing '$TaskName' task so this run is idempotent." -ForegroundColor Yellow
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$ScriptPath`""

# Starts ~1 minute from now, then repeats every 3 minutes indefinitely
# (RepetitionDuration uses a long-but-finite span -- [TimeSpan]::MaxValue
# has been unreliable with the underlying Task Scheduler COM API in
# practice; 10 years is effectively forever for this purpose).
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 3) -RepetitionDuration (New-TimeSpan -Days 3650)

# SYSTEM, not the logged-in user -- this must keep running even before
# anyone logs in (the whole point, matching the algo-* services
# themselves, which also run as SYSTEM/LocalSystem via NSSM).
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 2) -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings `
    -Description "Checks every 3 minutes that algo-django actually answers HTTP requests (not just that Windows reports its service as Running) and that every algo-* service's SCM status is Running, restarting anything found unhealthy. See watchdog_services.ps1 for the real incident this exists to prevent." | Out-Null

Write-Host "Installed. Verify with:" -ForegroundColor Green
Write-Host "  Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State"
Write-Host "Logs: logs\watchdog.log (only written to when it actually restarts something)" -ForegroundColor Yellow
Write-Host "To remove: Unregister-ScheduledTask -TaskName $TaskName" -ForegroundColor Yellow
