[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidatePattern('^[0-9a-fA-F]{40}$')]
    [string] $CommitSha,

    [string] $Region = 'us-east-1',
    [string] $Profile,
    [string] $ProjectTag = 'pz-server',
    [string] $EnvironmentTag = 'production',
    [ValidateRange(7200, 14400)]
    [int] $CommandTimeoutSeconds = 9000,
    [switch] $AllowConnectedPlayers
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\PZ.Common.ps1"

Assert-PZAwsCli
$Region = Get-PZEffectiveDefault $Region 'AWS_REGION' 'us-east-1' $PSBoundParameters 'Region'
$ProjectTag = Get-PZEffectiveDefault $ProjectTag 'PZ_PROJECT_TAG' 'pz-server' $PSBoundParameters 'ProjectTag'
$EnvironmentTag = Get-PZEffectiveDefault $EnvironmentTag 'PZ_ENVIRONMENT_TAG' 'production' $PSBoundParameters 'EnvironmentTag'

$instance = Get-PZInstance -Region $Region -Profile $Profile -ProjectTag $ProjectTag -EnvironmentTag $EnvironmentTag
$instanceId = [string] $instance.InstanceId
$state = [string] $instance.State.Name
if ($state -ne 'running') {
    throw "Deployment requires a running, SSM-managed host; instance $instanceId is '$state'. Start it normally first."
}
if (-not (Test-PZSsmOnline -InstanceId $instanceId -Region $Region -Profile $Profile)) {
    throw 'SSM is not online. Refusing deployment because the guarded host transaction cannot be observed.'
}

$normalizedSha = $CommitSha.ToLowerInvariant()
$deployArguments = $normalizedSha
if ($AllowConnectedPlayers) {
    Write-Warning 'Connected-player protection was explicitly disabled for this announced deployment; UNKNOWN state still blocks it.'
    $deployArguments += ' --allow-connected-players'
}
$deployCommand = "if [ -f /var/lib/pz-deploy/deployment-pending ]; then if [ ! -x /usr/local/sbin/pzctl-deploy-recovery ]; then echo 'Pending deployment has no preserved recovery orchestrator.' >&2; exit 75; fi; /usr/local/sbin/pzctl-deploy-recovery deploy $deployArguments; else /usr/local/bin/pzctl deploy $deployArguments; fi"

Write-Host "Deploying exact commit $normalizedSha through the guarded host lifecycle."
$commandId = Send-PZSsmCommand `
    -InstanceId $instanceId `
    -Region $Region `
    -Profile $Profile `
    -TimeoutSeconds $CommandTimeoutSeconds `
    -DeliveryTimeoutSeconds 300 `
    -Comment "Deploy pz-server commit $normalizedSha" `
    -Command $deployCommand
$invocation = Wait-PZSsmCommand `
    -CommandId $commandId `
    -InstanceId $instanceId `
    -Region $Region `
    -Profile $Profile `
    -TimeoutSeconds ($CommandTimeoutSeconds + 420)

if (-not [string]::IsNullOrWhiteSpace([string] $invocation.StandardOutputContent)) {
    $standardOutput = ([string] $invocation.StandardOutputContent).TrimEnd()
    Write-Host $standardOutput
}
if (-not [string]::IsNullOrWhiteSpace([string] $invocation.StandardErrorContent)) {
    $standardError = ([string] $invocation.StandardErrorContent).TrimEnd()
    Write-Host $standardError
}

$instance = Get-PZInstance -Region $Region -Profile $Profile -ProjectTag $ProjectTag -EnvironmentTag $EnvironmentTag
if ([string] $instance.State.Name -ne 'running') {
    throw 'The guarded deployment completed, but EC2 is no longer running; inspect the instance before proceeding.'
}

Write-Host ''
Write-Host 'Deployment completed successfully.'
Write-Host "Commit: $normalizedSha"
Write-Host "Instance: $instanceId"
Write-Host 'PZ: healthy and RCON-responsive'
