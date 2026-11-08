$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$pythonWindow = Join-Path $projectRoot '.venv\Scripts\pythonw.exe'
$pythonConsole = Join-Path $projectRoot '.venv\Scripts\python.exe'
$desktopPath = [Environment]::GetFolderPath('Desktop')
$startupPath = [Environment]::GetFolderPath('Startup')
$iconPath = Join-Path $projectRoot 'assets\marketing-manager.ico'
& $pythonConsole -m bot.make_icon
if ($LASTEXITCODE -ne 0) { throw 'Could not create the shortcut icon.' }
# One icon: it opens the Marketing Manager window and the bot runs while that window is open.
# A copy in the Startup folder opens it automatically when you log in to this VPS.
$shortcutShell = New-Object -ComObject WScript.Shell
foreach ($folder in @($desktopPath, $startupPath)) {
    $shortcut = $shortcutShell.CreateShortcut((Join-Path $folder 'Marketing Manager.lnk'))
    $shortcut.TargetPath = $pythonWindow
    $shortcut.Arguments = '-m bot.launcher'
    $shortcut.WorkingDirectory = $projectRoot
    $shortcut.IconLocation = $iconPath
    $shortcut.Save()
}
# Older installs had a separate Stop icon and a hidden boot task; closing the window replaces both.
Remove-Item -LiteralPath (Join-Path $desktopPath 'Stop Marketing Manager.lnk') -ErrorAction SilentlyContinue
if (Get-ScheduledTask -TaskName 'Marketing Manager' -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName 'Marketing Manager' -Confirm:$false
}
Write-Host 'Desktop icon created (it also opens when you log in). Close the window to stop the bot.'
Write-Host 'No other tasks or processes were changed.'
