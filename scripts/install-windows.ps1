$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$pythonWindow = Join-Path $projectRoot '.venv\Scripts\pythonw.exe'
$pythonConsole = Join-Path $projectRoot '.venv\Scripts\python.exe'
$desktopPath = [Environment]::GetFolderPath('Desktop')
$iconPath = Join-Path $projectRoot 'assets\marketing-manager.ico'
& $pythonConsole -m bot.make_icon
if ($LASTEXITCODE -ne 0) { throw 'Could not create the shortcut icon.' }
$shortcutShell = New-Object -ComObject WScript.Shell
foreach ($entry in @(@('Marketing Manager','-m bot.launcher'),@('Stop Marketing Manager','-m bot.launcher --stop'))) {
    $shortcut = $shortcutShell.CreateShortcut((Join-Path $desktopPath ($entry[0] + '.lnk')))
    $shortcut.TargetPath = $pythonWindow
    $shortcut.Arguments = $entry[1]
    $shortcut.WorkingDirectory = $projectRoot
    $shortcut.IconLocation = $iconPath
    $shortcut.Save()
}
Write-Host 'The boot task needs your Windows login so it can run while you are logged out.'
Write-Host 'Your password is handed to Windows Task Scheduler; the bot never stores it.'
$taskCredential = Get-Credential -UserName ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -Message 'Windows account for Marketing Manager startup task'
if ($null -eq $taskCredential) { throw 'Boot task was not registered. Rerun install.bat to finish setup.' }
$taskAction = New-ScheduledTaskAction -Execute $pythonWindow -Argument '-m bot' -WorkingDirectory $projectRoot
$taskTrigger = New-ScheduledTaskTrigger -AtStartup
$taskTrigger.Delay = 'PT45S'
$taskSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
# Priority 7 = BelowNormal. The bot also sets its own process priority explicitly.
$taskSettings.Priority = 7
$taskPrincipal = New-ScheduledTaskPrincipal -UserId $taskCredential.UserName -LogonType Password -RunLevel Limited
$taskDefinition = New-ScheduledTask -Action $taskAction -Trigger $taskTrigger -Settings $taskSettings -Principal $taskPrincipal
Register-ScheduledTask -TaskName 'Marketing Manager' -InputObject $taskDefinition -User $taskCredential.UserName -Password $taskCredential.GetNetworkCredential().Password -Force | Out-Null
Write-Host 'Desktop shortcuts and boot task created. No other tasks or processes were changed.'
