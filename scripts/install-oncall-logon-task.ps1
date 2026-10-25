[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$TaskName = 'OnCall-Managed-Logon',
    [switch]$Remove
)
$ErrorActionPreference = 'Stop'
$ProjectRoot = [IO.Path]::GetFullPath($ProjectRoot)
$scriptPath = Join-Path $ProjectRoot 'scripts\oncall-windows.ps1'
if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) { throw 'Managed launcher missing.' }
if ($Remove) {
    if ($PSCmdlet.ShouldProcess($TaskName, 'Unregister this user logon task')) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    }
    return
}
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    throw 'Task name already exists. Inspect it before replacing; no task was overwritten.'
}
$identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$powershellPath = (Get-Process -Id $PID).Path
# The script path is one literal argument; never compose a shell pipeline.
$actionArguments = '-NoProfile -NonInteractive -WindowStyle Hidden -File "' + $scriptPath.Replace('"','\"') + '" start -Foreground'
$action = New-ScheduledTaskAction -Execute $powershellPath -Argument $actionArguments -WorkingDirectory $ProjectRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
if ($PSCmdlet.ShouldProcess($TaskName, 'Register a current-user logon task (requires Docker Desktop already available)')) {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings
}
