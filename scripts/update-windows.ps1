$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $projectRoot
$pythonConsole = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot '.git'))) {
    throw 'This is a ZIP download. Download the new ZIP and copy bot/, scripts/, requirements.txt and batch files into this folder. Preserve config.yaml, campaigns/, secrets.txt and data/. See README.'
}
$dirtyFiles = & git status --porcelain --untracked-files=no
if ($LASTEXITCODE -ne 0) { throw 'Could not inspect repository.' }
if ($dirtyFiles) { throw 'You have edited tracked files, such as campaign configuration. Save/commit those changes before updating; update will not overwrite them.' }
& $pythonConsole -c "from bot.runtime import is_running; raise SystemExit(0 if is_running() else 1)"
$wasRunning = $LASTEXITCODE -eq 0
if ($wasRunning) {
    $stopPath = Join-Path $projectRoot 'data\stop.request'
    Set-Content -LiteralPath $stopPath -Value 'stop' -Encoding ascii
    $stopDeadline = (Get-Date).AddMinutes(5)
    do {
        Start-Sleep -Seconds 2
        & $pythonConsole -c "from bot.runtime import is_running; raise SystemExit(0 if is_running() else 1)"
        $stillRunning = $LASTEXITCODE -eq 0
    } while ($stillRunning -and (Get-Date) -lt $stopDeadline)
    if ($stillRunning) { throw 'The bot is still completing an action. Update cancelled; no process was killed. Try later.' }
}
try {
    & git pull --ff-only
    if ($LASTEXITCODE -ne 0) { throw 'Git update failed. Resolve the reported repository issue before trying again.' }
    & $pythonConsole -m pip install --disable-pip-version-check -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw 'Dependency update failed.' }
    $env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $projectRoot '.playwright'
    & $pythonConsole -m playwright install chromium --only-shell
    if ($LASTEXITCODE -ne 0) { throw 'Renderer update failed.' }
    & $pythonConsole -m bot.fonts
    if ($LASTEXITCODE -ne 0) { throw 'Font update failed.' }
} finally {
    if ($wasRunning) {
        & $pythonConsole -m bot.launcher --start-only
    }
}
Write-Host 'Marketing Manager updated. Other processes were untouched.'
