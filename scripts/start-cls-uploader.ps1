$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskPython = Join-Path $taskRoot 'tools\cls-uploader\.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) {
    throw 'Run uv sync --project tools/cls-uploader --python 3.11 first.'
}
$taskLogs = Join-Path $taskRoot 'logs'
New-Item -ItemType Directory -Path $taskLogs -Force | Out-Null
$taskStamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
Start-Process -FilePath $taskPython -ArgumentList '-m','oncall_logging.uploader' `
    -WorkingDirectory $taskRoot -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $taskLogs "cls-uploader-$taskStamp.stdout.log") `
    -RedirectStandardError (Join-Path $taskLogs "cls-uploader-$taskStamp.stderr.log") | Out-Null
Write-Output 'CLS upload process launched; see logs/cls-uploader-*.stdout.log for readiness.'
