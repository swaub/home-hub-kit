$ErrorActionPreference = "SilentlyContinue"
$dir = Join-Path $env:ProgramData "HomeHub"
Stop-ScheduledTask -TaskName "Home Hub PC agent"
Unregister-ScheduledTask -TaskName "Home Hub PC agent" -Confirm:$false
Remove-NetFirewallRule -DisplayName "Home Hub PC agent"
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match "homehub.pc_agent" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Remove-Item (Join-Path $dir "agent") -Recurse -Force
Remove-Item (Join-Path $dir "python") -Recurse -Force
Write-Host "Home Hub PC agent removed. The key file $dir\agent.toml was left in place; delete it to forget the key."
