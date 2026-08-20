[CmdletBinding()]
param(
    [ValidateSet('Normal', 'Party')]
    [string] $Mode = 'Normal',

    [string] $Region = 'us-east-1',
    [string] $Profile,
    [string] $ProjectTag = 'pz-server',
    [string] $EnvironmentTag = 'production',
    [string] $NormalInstanceType = 'r7a.large',
    [string] $PartyInstanceType = 'm7a.xlarge',
    [ValidateRange(30, 7200)]
    [int] $ReadyTimeoutSeconds = 7200
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\PZ.Common.ps1"

Assert-PZAwsCli
$Region = Get-PZEffectiveDefault $Region 'AWS_REGION' 'us-east-1' $PSBoundParameters 'Region'
$ProjectTag = Get-PZEffectiveDefault $ProjectTag 'PZ_PROJECT_TAG' 'pz-server' $PSBoundParameters 'ProjectTag'
$EnvironmentTag = Get-PZEffectiveDefault $EnvironmentTag 'PZ_ENVIRONMENT_TAG' 'production' $PSBoundParameters 'EnvironmentTag'

$instance = Get-PZInstance -Region $Region -Profile $Profile -ProjectTag $ProjectTag -EnvironmentTag $EnvironmentTag
$taggedNormalType = Get-PZInstanceTag -Instance $instance -Name 'PZNormalInstanceType'
$taggedPartyType = Get-PZInstanceTag -Instance $instance -Name 'PZPartyInstanceType'
if ([string]::IsNullOrWhiteSpace($taggedNormalType)) { $taggedNormalType = 'r7a.large' }
if ([string]::IsNullOrWhiteSpace($taggedPartyType)) { $taggedPartyType = 'm7a.xlarge' }
$NormalInstanceType = Get-PZEffectiveDefault $NormalInstanceType 'NORMAL_INSTANCE_TYPE' $taggedNormalType $PSBoundParameters 'NormalInstanceType'
$PartyInstanceType = Get-PZEffectiveDefault $PartyInstanceType 'PARTY_INSTANCE_TYPE' $taggedPartyType $PSBoundParameters 'PartyInstanceType'
$requestedType = if ($Mode -eq 'Party') { $PartyInstanceType } else { $NormalInstanceType }
$allowedTypes = @($NormalInstanceType, $PartyInstanceType) | Select-Object -Unique
if ($requestedType -notin $allowedTypes) {
    throw "Requested instance type '$requestedType' is outside the explicit allowlist: $($allowedTypes -join ', ')"
}
$instanceId = [string] $instance.InstanceId
$state = [string] $instance.State.Name

if ($state -eq 'stopping') {
    Write-Host "Instance $instanceId is stopping; waiting before any resize."
    Invoke-PZAwsWaiter -Waiter 'instance-stopped' -InstanceId $instanceId -Region $Region -Profile $Profile
    $instance = Get-PZInstance -Region $Region -Profile $Profile -ProjectTag $ProjectTag -EnvironmentTag $EnvironmentTag
    $state = [string] $instance.State.Name
}
if ($state -eq 'pending') {
    Invoke-PZAwsWaiter -Waiter 'instance-running' -InstanceId $instanceId -Region $Region -Profile $Profile
    $instance = Get-PZInstance -Region $Region -Profile $Profile -ProjectTag $ProjectTag -EnvironmentTag $EnvironmentTag
    $state = [string] $instance.State.Name
}

$currentType = [string] $instance.InstanceType
if ($state -eq 'running' -and $currentType -ne $requestedType) {
    throw "Instance is running as $currentType. AWS cannot resize it live; stop it gracefully before selecting $requestedType."
}
if ($state -eq 'stopped' -and $currentType -ne $requestedType) {
    Write-Host "Changing stopped instance from $currentType to $requestedType ($Mode mode)."
    $null = Invoke-PZAwsJson -Region $Region -Profile $Profile -Arguments @(
        'ec2', 'modify-instance-attribute',
        '--instance-id', $instanceId,
        '--instance-type', "Value=$requestedType"
    )
    $currentType = $requestedType
}
if ($state -eq 'stopped') {
    Write-Host "Starting instance $instanceId."
    $null = Invoke-PZAwsJson -Region $Region -Profile $Profile -Arguments @(
        'ec2', 'start-instances', '--instance-ids', $instanceId
    )
}
elseif ($state -ne 'running') {
    throw "Cannot start instance from unexpected state '$state'."
}
else {
    Write-Host "Instance $instanceId is already running; no redundant start request was sent."
}

Invoke-PZAwsWaiter -Waiter 'instance-running' -InstanceId $instanceId -Region $Region -Profile $Profile
Invoke-PZAwsWaiter -Waiter 'instance-status-ok' -InstanceId $instanceId -Region $Region -Profile $Profile
Wait-PZSsmOnline -InstanceId $instanceId -Region $Region -Profile $Profile -TimeoutSeconds 600

$bootstrapWaitSeconds = $ReadyTimeoutSeconds + 600
$readyCommand = "timeout $bootstrapWaitSeconds bash -lc 'until [ -x /usr/local/bin/pzctl ]; do sleep 10; done; unit_state=`$(systemctl is-active pz-stack.service 2>/dev/null || true); case `$unit_state in active|activating) ;; *) /usr/local/bin/pzctl start >&2 ;; esac; /usr/local/bin/pzctl wait-ready --timeout $ReadyTimeoutSeconds'"
$commandId = Send-PZSsmCommand `
    -InstanceId $instanceId `
    -Region $Region `
    -Profile $Profile `
    -TimeoutSeconds ($bootstrapWaitSeconds + 60) `
    -DeliveryTimeoutSeconds 120 `
    -Comment 'Wait for Project Zomboid readiness' `
    -Command $readyCommand
$invocation = Wait-PZSsmCommand `
    -CommandId $commandId `
    -InstanceId $instanceId `
    -Region $Region `
    -Profile $Profile `
    -TimeoutSeconds ($bootstrapWaitSeconds + 210)
$updateText = ([string] $invocation.StandardOutputContent).Trim()
if ([string]::IsNullOrWhiteSpace($updateText)) {
    throw 'Host readiness completed without authoritative game update status.'
}
$update = $updateText | ConvertFrom-Json
$updateState = [string] $update.state
if ($updateState -notin @('current', 'failed-before-world-open')) {
    throw "Host readiness returned unsafe game update state '$updateState'."
}

$instance = Get-PZInstance -Region $Region -Profile $Profile -ProjectTag $ProjectTag -EnvironmentTag $EnvironmentTag
$publicIp = [string] $instance.PublicIpAddress
if ([string]::IsNullOrWhiteSpace($publicIp)) {
    throw 'EC2 is running and PZ is healthy, but no public IPv4 address was assigned.'
}

Write-Host ''
Write-Host 'Server started successfully.'
Write-Host "Public IP: $publicIp"
Write-Host "Instance: $($instance.InstanceType)"
Write-Host 'State: running'
Write-Host 'PZ: healthy'
Write-Host "Steam build: $($update.current_build)"
Write-Host "Update state: $updateState"
if (-not [string]::IsNullOrWhiteSpace([string] $update.pz_version)) {
    Write-Host "PZ version: $($update.pz_version)"
}
if ($updateState -eq 'failed-before-world-open') {
    Write-Warning "Stable update was rejected before world access: $($update.detail)"
}
