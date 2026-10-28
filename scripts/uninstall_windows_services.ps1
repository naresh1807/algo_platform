<#
.SYNOPSIS
    Removes the 5 Windows Services installed by
    scripts\install_as_windows_services.ps1. Does not touch Docker/MySQL/
    Redis or the "start Docker Desktop on login" setting -- undo those
    separately (Docker Desktop Settings > General) if you want to fully
    revert to running the platform only via scripts\start_platform.ps1.

.DESCRIPTION
    Must be run from an elevated (Administrator) PowerShell, same as the
    install script.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\uninstall_windows_services.ps1
#>

[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$currentPrincipal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $currentPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "This script must be run from an elevated (Administrator) PowerShell."
}

function Find-Nssm {
    $onPath = Get-Command nssm -ErrorAction SilentlyContinue
    if ($onPath) { return $onPath.Source }
    $candidates = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\NSSM.NSSM_*\*\win64\nssm.exe" -ErrorAction SilentlyContinue
    if ($candidates) { return $candidates[0].FullName }
    throw "nssm.exe not found."
}
$Nssm = Find-Nssm

$Names = @("algo-django", "algo-celery-worker-default", "algo-celery-worker-priority", "algo-celery-beat", "algo-run-live-feed")

foreach ($name in $Names) {
    # Get-Service/Stop-Service, not `nssm status`/`nssm stop` -- see
    # install_as_windows_services.ps1's matching comment: redirecting a
    # native command's stderr (even `2>$null`) gets converted into a
    # terminating error under $ErrorActionPreference = "Stop" in
    # Windows PowerShell 5.1, which broke this exact pattern on first use.
    $existingService = Get-Service -Name $name -ErrorAction SilentlyContinue
    if (-not $existingService) {
        Write-Host "  $name not installed -- skipping." -ForegroundColor Yellow
        continue
    }
    Write-Host "==> Removing service: $name" -ForegroundColor Cyan
    if ($existingService.Status -ne "Stopped") {
        Stop-Service -Name $name -ErrorAction SilentlyContinue
    }
    & $Nssm remove $name confirm | Out-Null
    Write-Host "  removed." -ForegroundColor Green
}

Write-Host ""
Write-Host "All 5 services removed. The platform now needs scripts\start_platform.ps1 run manually again." -ForegroundColor Cyan
