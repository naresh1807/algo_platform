<#
.SYNOPSIS
    Reverses everything install_as_windows_services.ps1 +
    install_watchdog_task.ps1 + the auto-login step set up (see
    docs\running_as_a_server.md) -- run this when moving off "this PC
    is the always-on server" (e.g. moving to a cloud-hosted backend
    instead, see docs\desktop_and_android_apps.md's cloud section).

.DESCRIPTION
    Deliberately does NOT touch:
      - Docker Desktop's "start on login" setting
      - The WSL2 memory cap (.wslconfig)
      - AC sleep/hibernate settings (powercfg)
    All three are harmless/beneficial regardless of where the backend
    ends up running, so they're left as-is unless you ask otherwise.

    scripts\start_platform.ps1 still works afterwards for running the
    backend locally by hand (e.g. testing before a cloud deploy) --
    this script only removes the unattended-24/7 wrapping around it.

    Must be run from an elevated (Administrator) PowerShell, same as
    the install scripts it undoes.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\remove_server_setup.ps1
#>

[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$currentPrincipal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $currentPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "This script must be run from an elevated (Administrator) PowerShell."
}

Write-Host "==> Removing the 5 Windows Services..." -ForegroundColor Cyan
& (Join-Path $PSScriptRoot "uninstall_windows_services.ps1")

Write-Host ""
Write-Host "==> Removing the watchdog scheduled task..." -ForegroundColor Cyan
if (Get-ScheduledTask -TaskName "AlgoPlatformWatchdog" -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName "AlgoPlatformWatchdog" -Confirm:$false
    Write-Host "  Removed." -ForegroundColor Green
} else {
    Write-Host "  Not installed -- nothing to do." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "==> Disabling Windows auto-login..." -ForegroundColor Cyan
$winlogonPath = "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon"
$current = Get-ItemProperty -Path $winlogonPath -Name AutoAdminLogon -ErrorAction SilentlyContinue
if ($current -and $current.AutoAdminLogon -eq "1") {
    Set-ItemProperty -Path $winlogonPath -Name AutoAdminLogon -Value "0"
    Remove-ItemProperty -Path $winlogonPath -Name DefaultPassword -ErrorAction SilentlyContinue
    Write-Host "  Auto-login disabled (the stored password is removed from the registry)." -ForegroundColor Green
} else {
    Write-Host "  Auto-login was not enabled -- nothing to do." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "===================================================================" -ForegroundColor Cyan
Write-Host " Local 24/7-server setup removed." -ForegroundColor Cyan
Write-Host " Not touched (harmless/beneficial regardless of where the backend" -ForegroundColor Cyan
Write-Host " runs -- ask if you want any of these reverted too):" -ForegroundColor Cyan
Write-Host "   - Docker Desktop 'start on login' setting"
Write-Host "   - WSL2 memory cap (.wslconfig)"
Write-Host "   - AC sleep/hibernate settings (powercfg)"
Write-Host ""
Write-Host " scripts\start_platform.ps1 still works for running the backend" -ForegroundColor Yellow
Write-Host " locally by hand whenever you need to." -ForegroundColor Yellow
Write-Host "===================================================================" -ForegroundColor Cyan
