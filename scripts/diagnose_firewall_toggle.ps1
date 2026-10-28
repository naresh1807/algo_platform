<#
.SYNOPSIS
    TEMPORARY diagnostic only. Turns off the Windows Firewall for the
    Private profile, waits for you to test from the phone, then turns
    it back on. Run elevated.
#>

Write-Host "=== Turning OFF Windows Firewall for the Private profile (TEMPORARY) ===" -ForegroundColor Yellow
Set-NetFirewallProfile -Profile Private -Enabled False

Write-Host ""
Write-Host "Now try 'Test Connection' on your phone RIGHT NOW." -ForegroundColor Cyan
Write-Host "Press Enter here once you've tried it (either result)." -ForegroundColor Cyan
Read-Host

Write-Host "=== Turning Windows Firewall back ON for the Private profile ===" -ForegroundColor Yellow
Set-NetFirewallProfile -Profile Private -Enabled True

Get-NetFirewallProfile -Profile Private | Select-Object Name, Enabled
Write-Host "Firewall restored. Tell Claude what happened on the phone during the window it was off." -ForegroundColor Green
