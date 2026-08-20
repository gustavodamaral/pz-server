[CmdletBinding()]
param(
    [string] $Region = 'us-east-1',
    [string] $Profile,
    [string] $ProjectTag = 'pz-server',
    [string] $EnvironmentTag = 'production'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\PZ.Common.ps1"

function Format-Bytes {
    param([AllowNull()][object] $Value)
    if ($null -eq $Value) { return 'unavailable' }
    $number = [double] $Value
    if ($number -ge 1TB) { return '{0:N1} TiB' -f ($number / 1TB) }
    if ($number -ge 1GB) { return '{0:N1} GiB' -f ($number / 1GB) }
    if ($number -ge 1MB) { return '{0:N1} MiB' -f ($number / 1MB) }
    return '{0:N0} B' -f $number
}

function Format-Duration {
    param([AllowNull()][object] $Seconds)
    if ($null -eq $Seconds) { return 'unavailable' }
    $span = [TimeSpan]::FromSeconds([Math]::Max(0, [double] $Seconds))
    if ($span.Days -gt 0) { return '{0}d {1}h {2}m' -f $span.Days, $span.Hours, $span.Minutes }
    return '{0}h {1}m' -f [Math]::Floor($span.TotalHours), $span.Minutes
}

function Get-OptionalProperty {
    param(
        [AllowNull()][object] $Object,
        [Parameter(Mandatory)][string] $Name
    )
    if ($null -eq $Object) { return $null }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) { return $null }
    return $property.Value
}

Assert-PZAwsCli
$Region = Get-PZEffectiveDefault $Region 'AWS_REGION' 'us-east-1' $PSBoundParameters 'Region'
$ProjectTag = Get-PZEffectiveDefault $ProjectTag 'PZ_PROJECT_TAG' 'pz-server' $PSBoundParameters 'ProjectTag'
$EnvironmentTag = Get-PZEffectiveDefault $EnvironmentTag 'PZ_ENVIRONMENT_TAG' 'production' $PSBoundParameters 'EnvironmentTag'
$instance = Get-PZInstance -Region $Region -Profile $Profile -ProjectTag $ProjectTag -EnvironmentTag $EnvironmentTag
$state = [string] $instance.State.Name

Write-Host "EC2: $state"
Write-Host "Instance: $($instance.InstanceType)"
if ($state -eq 'stopped') {
    Write-Host 'PZ: offline'
    return
}
if ($state -ne 'running') {
    Write-Host 'PZ: transitional/unavailable'
    return
}

$publicIp = [string] $instance.PublicIpAddress
Write-Host "Public IP: $publicIp"
$launch = [DateTimeOffset]::Parse([string] $instance.LaunchTime)
Write-Host "EC2 uptime: $(Format-Duration ([DateTimeOffset]::UtcNow.Subtract($launch).TotalSeconds))"

if (-not (Test-PZSsmOnline -InstanceId $instance.InstanceId -Region $Region -Profile $Profile)) {
    Write-Host 'SSM: offline'
    Write-Host 'PZ: UNKNOWN (no unsafe inference made)'
    return
}

try {
    $commandId = Send-PZSsmCommand `
        -InstanceId $instance.InstanceId `
        -Region $Region `
        -Profile $Profile `
        -TimeoutSeconds 60 `
        -DeliveryTimeoutSeconds 30 `
        -Comment 'Collect Project Zomboid status' `
        -Command '/usr/local/bin/pzctl status --json'
    $invocation = Wait-PZSsmCommand `
        -CommandId $commandId `
        -InstanceId $instance.InstanceId `
        -Region $Region `
        -Profile $Profile `
        -TimeoutSeconds 120
    $metrics = ([string] $invocation.StandardOutputContent).Trim() | ConvertFrom-Json
}
catch {
    Write-Host 'PZ: UNKNOWN'
    Write-Warning "Status collection failed safely: $($_.Exception.Message)"
    return
}

$playerText = if ($null -eq $metrics.players) { 'UNKNOWN' } else { [string] $metrics.players }
Write-Host "PZ: $($metrics.status)"
Write-Host "Players: $playerText"
Write-Host ''
$update = Get-OptionalProperty -Object $metrics -Name 'update'
if ($null -eq $update) {
    Write-Host 'Update: unavailable (host may run an older repository version)'
}
else {
    $updateState = Get-OptionalProperty -Object $update -Name 'state'
    $currentBuild = Get-OptionalProperty -Object $update -Name 'current_build'
    $candidateBuild = Get-OptionalProperty -Object $update -Name 'candidate_build'
    $policy = Get-OptionalProperty -Object $update -Name 'update_policy'
    $branch = Get-OptionalProperty -Object $update -Name 'steam_branch'
    $lastResult = Get-OptionalProperty -Object $update -Name 'last_result'
    $version = Get-OptionalProperty -Object $update -Name 'pz_version'
    $revision = Get-OptionalProperty -Object $update -Name 'pz_revision'
    $detail = Get-OptionalProperty -Object $update -Name 'detail'
    if ($null -eq $currentBuild) { $currentBuild = 'unavailable' }
    if ($null -eq $candidateBuild) { $candidateBuild = 'none' }
    Write-Host 'Update:'
    Write-Host "  State: $updateState"
    Write-Host "  Steam build: $currentBuild"
    Write-Host "  Candidate: $candidateBuild"
    Write-Host "  Policy/branch: $policy / $branch"
    Write-Host "  Last result: $lastResult"
    if ($null -ne $version) { Write-Host "  PZ version: $version" }
    if ($null -ne $revision) { Write-Host "  Revision: $revision" }
    if ($updateState -like 'failed-*' -or $updateState -eq 'unknown') {
        Write-Warning "Game update requires attention: $detail"
    }
}
Write-Host ''
Write-Host 'CPU:'
Write-Host ('  Total: {0:N1}%' -f [double] $metrics.cpu_total_percent)
Write-Host ('  Hottest core: {0:N1}%' -f [double] $metrics.hottest_core_percent)
Write-Host 'Memory:'
Write-Host "  PZ process: $(Format-Bytes $metrics.pz_process_memory_bytes)"
Write-Host "  System: $(Format-Bytes $metrics.system_memory_used_bytes) / $(Format-Bytes $metrics.system_memory_total_bytes)"
Write-Host "  Available: $(Format-Bytes $metrics.system_memory_available_bytes)"
Write-Host 'Uptime:'
Write-Host "  Host: $(Format-Duration $metrics.server_uptime_seconds)"
Write-Host "  Container: $(Format-Duration $metrics.container_uptime_seconds)"
Write-Host 'Disk:'
Write-Host "  Used: $(Format-Bytes $metrics.disk_used_bytes) / $(Format-Bytes $metrics.disk_total_bytes)"
