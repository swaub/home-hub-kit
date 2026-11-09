param([string]$Bind = "0.0.0.0", [int]$Port = 7781, [switch]$NoFixes)
$ErrorActionPreference = "Stop"
$PythonVersion = "3.13.17"
$PythonChecksums = @{ amd64 = "9e35319bffedab8b36ad44d8f377cb4f1733bcec257a0b6d04d94f01486ddb38"; arm64 = "7b58787a047b400edbfcb539aa3cdb4e2ed77bf0f22cae42b7c01cedd7dd6614" }
$identity = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $identity.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  Write-Host "Run this from an administrator PowerShell (right-click PowerShell, 'Run as administrator'), then:"
  Write-Host "  powershell -ExecutionPolicy Bypass -File `"$($MyInvocation.MyCommand.Path)`""
  exit 1
}
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$source = @((Join-Path $here "pc_agent"), (Join-Path (Split-Path -Parent $here) "homehub\pc_agent")) | Where-Object { Test-Path (Join-Path $_ "server.py") } | Select-Object -First 1
if (-not $source) { Write-Host "The agent files were not found next to this script (pc_agent\ or ..\homehub\pc_agent\)."; exit 1 }
$dir = Join-Path $env:ProgramData "HomeHub"
$agentDir = Join-Path $dir "agent"
$pythonDir = Join-Path $dir "python"
$configPath = Join-Path $dir "agent.toml"
$notesPath = Join-Path $dir "install-notes.txt"
New-Item -ItemType Directory -Force -Path $dir | Out-Null
Stop-ScheduledTask -TaskName "Home Hub PC agent" -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match "homehub.pc_agent" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
$package = Join-Path $agentDir "homehub\pc_agent"
New-Item -ItemType Directory -Force -Path $package | Out-Null
Copy-Item (Join-Path $source "*.py") -Destination $package -Force
Copy-Item (Join-Path $source "*.ps1") -Destination $package -Force
New-Item -ItemType File -Force -Path (Join-Path $agentDir "homehub\__init__.py") | Out-Null
$arch = if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") { "arm64" } else { "amd64" }
$python = Join-Path $pythonDir "pythonw.exe"
$versionFile = Join-Path $pythonDir "version.txt"
$wanted = "$PythonVersion-$arch"
$have = if (Test-Path $versionFile) { (Get-Content $versionFile -Raw).Trim() } else { "" }
if (-not (Test-Path $python) -or $have -ne $wanted) {
  $zipName = "python-$PythonVersion-embed-$arch.zip"
  $zip = Join-Path $dir $zipName
  Write-Host "Downloading Python $PythonVersion ($arch) from python.org..."
  [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
  Invoke-WebRequest -Uri "https://www.python.org/ftp/python/$PythonVersion/$zipName" -OutFile $zip -UseBasicParsing
  $hash = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
  if ($hash -ne $PythonChecksums[$arch]) { Remove-Item $zip -Force; throw "The Python download did not match its pinned checksum ($hash); nothing was installed." }
  if (Test-Path $pythonDir) { Remove-Item $pythonDir -Recurse -Force }
  Expand-Archive -Path $zip -DestinationPath $pythonDir
  Remove-Item $zip -Force
  Set-Content -Path $versionFile -Value $wanted -Encoding ASCII
}
$pth = Get-ChildItem $pythonDir -Filter "python*._pth" | Select-Object -First 1
$lines = @(Get-Content $pth.FullName | Where-Object { $_ -ne "..\agent" })
Set-Content -Path $pth.FullName -Value ($lines + "..\agent") -Encoding ASCII
if (Test-Path $configPath) {
  $key = (Select-String -Path $configPath -Pattern '^key = "(.+)"').Matches[0].Groups[1].Value
} else {
  $bytes = New-Object byte[] 32; [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
  $key = ($bytes | ForEach-Object { $_.ToString("x2") }) -join ""
}
$allow = if ($NoFixes) { "false" } else { "true" }
$utf8 = New-Object System.Text.UTF8Encoding $false
[System.IO.File]::WriteAllText($configPath, "[agent]`nname = `"$env:COMPUTERNAME`"`nbind = `"$Bind`"`nport = $Port`nallow_fixes = $allow`nkey = `"$key`"`n", $utf8)
$acl = Get-Acl $configPath
$acl.SetAccessRuleProtection($true, $false)
$acl.Access | ForEach-Object { $acl.RemoveAccessRule($_) | Out-Null }
foreach ($who in @("BUILTIN\Administrators", "NT AUTHORITY\SYSTEM", $env:USERNAME)) {
  $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule($who, "FullControl", "Allow")))
}
Set-Acl $configPath $acl
$action = New-ScheduledTaskAction -Execute $python -Argument "-m homehub.pc_agent --config `"$configPath`"" -WorkingDirectory $agentDir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Days 3650)
Register-ScheduledTask -TaskName "Home Hub PC agent" -Action $action -Trigger $trigger -Settings $settings -RunLevel Highest -Force | Out-Null
Remove-NetFirewallRule -DisplayName "Home Hub PC agent" -ErrorAction SilentlyContinue
New-NetFirewallRule -DisplayName "Home Hub PC agent" -Direction Inbound -Protocol TCP -LocalPort $Port -Program $python -RemoteAddress @("LocalSubnet", "100.64.0.0/10") -Action Allow -Profile Any | Out-Null
Start-ScheduledTask -TaskName "Home Hub PC agent"
$id = ($env:COMPUTERNAME.ToLower() -replace "[^a-z0-9]", "_")
if ($id -notmatch "^[a-z]") { $id = "pc_$id" }
$notes = @(
  "Home Hub PC agent installed on $env:COMPUTERNAME ($(Get-Date -Format 'yyyy-MM-dd HH:mm')), listening on ${Bind}:$Port (fixes: $allow).",
  "Files: $agentDir and $pythonDir; settings and key: $configPath.",
  "",
  "Add this computer to the hub. In config/people.toml:",
  "  [devices.$id]",
  "  owner = `"<person id>`"",
  "  kind = `"pc`"",
  "  name = `"<what the family calls it>`"",
  "  hostname = `"$env:COMPUTERNAME`"",
  "  capabilities = [`"pc_agent`", `"wake_on_lan`"]",
  "And in the hub's data folder secrets.toml:",
  "  [pc_agents]",
  "  $id = `"$key`"",
  "Then on the hub: homehub people import, and set the device's address with remember_device or last_ip."
)
[System.IO.File]::WriteAllText($notesPath, ($notes -join "`r`n") + "`r`n", $utf8)
Write-Host ""
$notes | ForEach-Object { Write-Host $_ }
Write-Host ""
Write-Host "These lines are also saved in $notesPath."
