[CmdletBinding()]
param(
    [string] $Region = 'us-east-1',
    [string] $Profile,
    [string] $ProjectTag = 'pz-server',
    [string] $EnvironmentTag = 'production',
    [switch] $AllowConnectedPlayers,
    [switch] $Force
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

if ($state -eq 'stopped') {
    Write-Host 'EC2: stopped'
    Write-Host 'PZ: offline'
    return
}
if ($state -eq 'stopping') {
    Write-Host "Instance $instanceId is already stopping; waiting for stopped state."
    Invoke-PZAwsWaiter -Waiter 'instance-stopped' -InstanceId $instanceId -Region $Region -Profile $Profile
    Write-Host 'EC2: stopped'
    Write-Host 'PZ: offline'
    return
}
if ($state -eq 'pending') {
    Write-Host "Instance $instanceId is pending; waiting for running state before a graceful stop."
    Invoke-PZAwsWaiter -Waiter 'instance-running' -InstanceId $instanceId -Region $Region -Profile $Profile
    if (-not $Force) {
        Wait-PZSsmOnline -InstanceId $instanceId -Region $Region -Profile $Profile -TimeoutSeconds 600
    }
}

if ($Force) {
    Write-Warning 'FORCE STOP bypasses the PZ save/quit path and can corrupt the world. Use only when SSM and graceful management are unavailable.'
    $null = Invoke-PZAwsJson -Region $Region -Profile $Profile -Arguments @(
        'ec2', 'stop-instances', '--instance-ids', $instanceId, '--force'
    )
}
else {
    if (-not (Test-PZSsmOnline -InstanceId $instanceId -Region $Region -Profile $Profile)) {
        throw 'SSM is not online. Refusing an implicit hard stop; investigate or rerun with the explicit -Force switch.'
    }
    Write-Host 'Requesting RCON save, graceful container stop, and guest Linux poweroff through SSM.'
    $stackStateCommand = 'systemctl show --property=ActiveState --value pz-stack.service'
    $stackStateId = Send-PZSsmCommand `
        -InstanceId $instanceId `
        -Region $Region `
        -Profile $Profile `
        -TimeoutSeconds 30 `
        -DeliveryTimeoutSeconds 30 `
        -Comment 'Inspect Project Zomboid startup state' `
        -Command $stackStateCommand
    $stackState = ([string] (Wait-PZSsmCommand `
        -CommandId $stackStateId `
        -InstanceId $instanceId `
        -Region $Region `
        -Profile $Profile `
        -TimeoutSeconds 60).StandardOutputContent).Trim()
    $shutdownCommand = '/usr/local/bin/pzctl shutdown-host'
    if ($stackState -eq 'activating') {
        Write-Host 'Cancelling in-progress PZ startup before guest poweroff.'
        $shutdownCommand = 'systemctl stop pz-watchdog.service pz-stack.service && systemctl poweroff'
    }
    elseif ($AllowConnectedPlayers) {
        Write-Warning 'Connected-player protection was explicitly disabled for this graceful maintenance stop.'
        $shutdownCommand += ' --allow-connected-players'
    }
    $commandId = Send-PZSsmCommand `
        -InstanceId $instanceId `
        -Region $Region `
        -Profile $Profile `
        -TimeoutSeconds 300 `
        -DeliveryTimeoutSeconds 60 `
        -Comment 'Gracefully stop Project Zomboid and power off host' `
        -Command $shutdownCommand
    $null = Wait-PZSsmCommand `
        -CommandId $commandId `
        -InstanceId $instanceId `
        -Region $Region `
        -Profile $Profile `
        -TimeoutSeconds 390
}

Invoke-PZAwsWaiter -Waiter 'instance-stopped' -InstanceId $instanceId -Region $Region -Profile $Profile
Write-Host ''
Write-Host 'Server stopped successfully.'
Write-Host 'EC2: stopped'
Write-Host 'PZ: offline'
