<#
.SYNOPSIS
    One-shot diagnostic + clean restart for the algo-* Windows Services.
    Run from an elevated PowerShell (same window used for
    install_as_windows_services.ps1).
#>

Write-Host "=== Current service status ===" -ForegroundColor Cyan
Get-Service algo-* | Select-Object Name, Status, StartType | Format-Table -AutoSize

Write-Host "=== Full command lines of every python.exe right now ===" -ForegroundColor Cyan
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Select-Object ProcessId, CreationDate, CommandLine | Format-List

Write-Host "=== Stopping all 5 services ===" -ForegroundColor Cyan
Stop-Service algo-django, algo-celery-worker-default, algo-celery-worker-priority, algo-celery-beat, algo-run-live-feed -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 3

Write-Host "=== Any python.exe survivors after stop (should be none of ours) ===" -ForegroundColor Cyan
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Select-Object ProcessId, CreationDate, CommandLine | Format-List

Write-Host "=== Starting all 5 services fresh ===" -ForegroundColor Cyan
Start-Service algo-django, algo-celery-worker-default, algo-celery-worker-priority, algo-celery-beat, algo-run-live-feed
Start-Sleep -Seconds 8

Write-Host "=== Status after fresh start ===" -ForegroundColor Cyan
Get-Service algo-* | Select-Object Name, Status | Format-Table -AutoSize

Write-Host "=== Latest algo-django error log (last 40 lines) ===" -ForegroundColor Cyan
$RepoRoot = Split-Path -Parent $PSScriptRoot
Get-Content (Join-Path $RepoRoot "logs\algo-django.service.err.log") -Tail 40 -ErrorAction SilentlyContinue

Write-Host "=== Health check ===" -ForegroundColor Cyan
try {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/monitoring/health/" -TimeoutSec 5 -UseBasicParsing
    Write-Host "HTTP $($r.StatusCode)" -ForegroundColor Green
} catch {
    Write-Host "FAILED: $($_.Exception.Message)" -ForegroundColor Red
}
