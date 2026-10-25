[CmdletBinding()]
param(
    [Parameter(Position=0)]
    [ValidateSet('start','stop','status','probe','backup','restore','drill','package','deploy','rollback')]
    [string]$Command = 'status',
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$DataRoot,
    [string]$ConfigRoot,
    [string]$PythonExe,
    [string]$DockerExe = 'docker',
    [string]$Target,
    [string]$Backup,
    [string]$Release,
    [string]$Version,
    [string[]]$Schema,
    [string]$Settings,
    [switch]$Once,
    [switch]$Bootstrap,
    [switch]$Foreground,
    [switch]$KeepResources
)
$ErrorActionPreference = 'Stop'
$ProjectRoot = [IO.Path]::GetFullPath($ProjectRoot)
if (-not $DataRoot) { $DataRoot = $ProjectRoot }
if (-not $ConfigRoot) { $ConfigRoot = $ProjectRoot }
$DataRoot = [IO.Path]::GetFullPath($DataRoot)
$ConfigRoot = [IO.Path]::GetFullPath($ConfigRoot)
if (-not $PythonExe) { $PythonExe = Join-Path $ProjectRoot '.venv\Scripts\python.exe' }
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw 'Project Python unavailable. Run uv sync or pass -PythonExe explicitly.'
}

# The control channel and local archives belong only to this Windows identity.
# This is not scheduled-task installation and requires no machine-wide service.
$operationsRoot = Join-Path $DataRoot '.runtime\operations'
New-Item -ItemType Directory -Path $operationsRoot -Force | Out-Null
if ($env:OS -eq 'Windows_NT') {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $acl = New-Object Security.AccessControl.DirectorySecurity
    $acl.SetOwner($identity)
    $acl.SetAccessRuleProtection($true, $false)
    $inheritance = [Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit'
    foreach ($sid in @($identity, [Security.Principal.SecurityIdentifier]'S-1-5-18')) {
        $rule = New-Object Security.AccessControl.FileSystemAccessRule($sid, 'FullControl', $inheritance, 'None', 'Allow')
        $acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $operationsRoot -AclObject $acl
}

$arguments = @('-m','oncall_ops.cli','--root',$ProjectRoot,'--data-root',$DataRoot,
               '--config-root',$ConfigRoot,'--python',$PythonExe,'--docker',$DockerExe,$Command)
switch ($Command) {
    'start' { if ($Foreground) { $arguments += '--foreground' } }
    'probe' {
        if ($Once) { $arguments += '--once' }
        if ($Settings) { $arguments += @('--settings',[IO.Path]::GetFullPath($Settings)) }
    }
    'backup' { if ($Target) { $arguments += @('--destination',[IO.Path]::GetFullPath($Target)) } }
    'restore' {
        if (-not $Backup -or -not $Target) { throw 'restore requires -Backup and -Target (new isolated directory).' }
        $arguments += @([IO.Path]::GetFullPath($Backup),'--target',[IO.Path]::GetFullPath($Target))
    }
    'drill' {
        if (-not $Target) { throw 'drill requires a new isolated -Target directory.' }
        $arguments += @('--target',[IO.Path]::GetFullPath($Target))
        if ($KeepResources) { $arguments += '--keep-resources' }
    }
    'package' {
        if (-not $Version -or -not $Schema) { throw 'package requires -Version and the compatible -Schema revision(s).' }
        $arguments += @('--version',$Version)
        foreach ($revision in $Schema) { $arguments += @('--schema',$revision) }
    }
    'deploy' {
        if (-not $Release) { throw 'deploy requires an immutable -Release directory.' }
        $arguments += [IO.Path]::GetFullPath($Release)
        if ($Bootstrap) { $arguments += '--bootstrap' }
    }
}
Push-Location -LiteralPath $ProjectRoot
try {
    & $PythonExe @arguments
    $operationExitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $operationExitCode
