<#
.SYNOPSIS
    Checks for firewall rules that could be blocking inbound access to
    the Django service on port 8000, and confirms the allow rule we
    added is actually scoped correctly. Run elevated.
#>

Write-Host "=== Our allow rule ===" -ForegroundColor Cyan
$ours = Get-NetFirewallRule -DisplayName "Algo Trading Platform - Django (8000)" -ErrorAction SilentlyContinue
if ($ours) {
    $ours | Select-Object DisplayName, Direction, Action, Profile, Enabled
    $ours | Get-NetFirewallPortFilter | Select-Object Protocol, LocalPort
} else {
    Write-Host "NOT FOUND -- the rule doesn't exist!" -ForegroundColor Red
}

Write-Host ""
Write-Host "=== ALL rules touching port 8000 (any action) ===" -ForegroundColor Cyan
Get-NetFirewallPortFilter -Protocol TCP | Where-Object { $_.LocalPort -eq 8000 -or $_.LocalPort -eq "Any" } |
    Get-NetFirewallRule | Select-Object DisplayName, Direction, Action, Profile, Enabled | Format-Table -AutoSize

Write-Host ""
Write-Host "=== ALL rules mentioning python.exe (any action) ===" -ForegroundColor Cyan
Get-NetFirewallApplicationFilter | Where-Object { $_.Program -like "*python*" } |
    Get-NetFirewallRule | Select-Object DisplayName, Direction, Action, Profile, Enabled | Format-Table -AutoSize

Write-Host ""
Write-Host "=== Windows Firewall itself -- is it even on, and for which profile? ===" -ForegroundColor Cyan
Get-NetFirewallProfile | Select-Object Name, Enabled, DefaultInboundAction

Write-Host ""
Write-Host "=== Actual live TCP listener on 8000 ===" -ForegroundColor Cyan
Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue | Select-Object LocalAddress, LocalPort
