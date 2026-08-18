Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Import-PZHelperEnvironment {
    $repositoryRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
    $environmentPath = Join-Path $repositoryRoot '.env'
    if (-not (Test-Path -LiteralPath $environmentPath -PathType Leaf)) {
        return
    }

    $allowedNames = @(
        'AWS_REGION',
        'NORMAL_INSTANCE_TYPE',
        'PARTY_INSTANCE_TYPE',
        'PZ_PROJECT_TAG',
        'PZ_ENVIRONMENT_TAG'
    )
    foreach ($line in [System.IO.File]::ReadLines($environmentPath)) {
        if ($line -notmatch '^\s*([A-Z][A-Z0-9_]*)=(.*)$') {
            continue
        }
        $name = $Matches[1]
        if ($name -notin $allowedNames) {
            continue
        }
        if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($name))) {
            [Environment]::SetEnvironmentVariable($name, $Matches[2], 'Process')
        }
    }
}

function Assert-PZAwsCli {
    if (-not (Get-Command -Name 'aws' -ErrorAction SilentlyContinue)) {
        throw 'AWS CLI v2 was not found on PATH.'
    }
}

function Invoke-PZAwsJson {
    param(
        [Parameter(Mandatory)]
        [string[]] $Arguments,

        [Parameter(Mandatory)]
        [string] $Region,

        [string] $Profile
    )

    $allArguments = [System.Collections.Generic.List[string]]::new()
    foreach ($argument in $Arguments) {
        $allArguments.Add($argument)
    }
    $allArguments.Add('--region')
    $allArguments.Add($Region)
    if ($Profile) {
        $allArguments.Add('--profile')
        $allArguments.Add($Profile)
    }
    $allArguments.Add('--no-cli-pager')
    $allArguments.Add('--cli-connect-timeout')
    $allArguments.Add('10')
    $allArguments.Add('--cli-read-timeout')
    $allArguments.Add('60')
    $allArguments.Add('--output')
    $allArguments.Add('json')
    $nativeArguments = $allArguments.ToArray()
    $output = @(& aws @nativeArguments 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw "AWS CLI failed: $($output -join [Environment]::NewLine)"
    }
    $text = $output -join [Environment]::NewLine
    if ([string]::IsNullOrWhiteSpace($text)) {
        return $null
    }
    return $text | ConvertFrom-Json
}

function Invoke-PZAwsWaiter {
    param(
        [Parameter(Mandatory)]
        [string] $Waiter,

        [Parameter(Mandatory)]
        [string] $InstanceId,

        [Parameter(Mandatory)]
        [string] $Region,

        [string] $Profile
    )

    $arguments = @(
        'ec2', 'wait', $Waiter,
        '--instance-ids', $InstanceId,
        '--region', $Region,
        '--no-cli-pager',
        '--cli-connect-timeout', '10',
        '--cli-read-timeout', '60'
    )
    if ($Profile) {
        $arguments += @('--profile', $Profile)
    }
    $output = @(& aws @arguments 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw "AWS waiter '$Waiter' failed: $($output -join [Environment]::NewLine)"
    }
}

function Get-PZInstance {
    param(
        [Parameter(Mandatory)]
        [string] $Region,

        [string] $Profile,
        [string] $ProjectTag = 'pz-server',
        [string] $EnvironmentTag = 'production'
    )

    $response = Invoke-PZAwsJson -Region $Region -Profile $Profile -Arguments @(
        'ec2', 'describe-instances',
        '--filters',
        "Name=tag:Project,Values=$ProjectTag",
        "Name=tag:Environment,Values=$EnvironmentTag",
        'Name=tag:Role,Values=game-server',
        'Name=instance-state-name,Values=pending,running,stopping,stopped'
    )
    $instances = @(
        foreach ($reservation in @($response.Reservations)) {
            foreach ($instance in @($reservation.Instances)) {
                $instance
            }
        }
    )
    if ($instances.Count -eq 0) {
        throw "No active EC2 instance matched Project=$ProjectTag, Environment=$EnvironmentTag, Role=game-server in $Region."
    }
    if ($instances.Count -ne 1) {
        $ids = @($instances | ForEach-Object { $_.InstanceId }) -join ', '
        throw "Expected exactly one tagged PZ instance but found $($instances.Count): $ids"
    }
    return $instances[0]
}

function Get-PZInstanceTag {
    param(
        [Parameter(Mandatory)]
        [object] $Instance,

        [Parameter(Mandatory)]
        [string] $Name
    )

    $tag = @($Instance.Tags) | Where-Object { $_.Key -eq $Name } | Select-Object -First 1
    if ($null -eq $tag) {
        return $null
    }
    return [string] $tag.Value
}

function Test-PZSsmOnline {
    param(
        [Parameter(Mandatory)]
        [string] $InstanceId,

        [Parameter(Mandatory)]
        [string] $Region,

        [string] $Profile
    )

    $response = Invoke-PZAwsJson -Region $Region -Profile $Profile -Arguments @(
        'ssm', 'describe-instance-information',
        '--filters', "Key=InstanceIds,Values=$InstanceId"
    )
    $match = @($response.InstanceInformationList) | Where-Object {
        $_.InstanceId -eq $InstanceId -and $_.PingStatus -eq 'Online'
    }
    return @($match).Count -eq 1
}

function Wait-PZSsmOnline {
    param(
        [Parameter(Mandatory)]
        [string] $InstanceId,

        [Parameter(Mandatory)]
        [string] $Region,

        [string] $Profile,
        [int] $TimeoutSeconds = 600
    )

    $deadline = [DateTimeOffset]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTimeOffset]::UtcNow -lt $deadline) {
        if (Test-PZSsmOnline -InstanceId $InstanceId -Region $Region -Profile $Profile) {
            return
        }
        Start-Sleep -Seconds 10
    }
    throw "SSM did not report instance $InstanceId online within $TimeoutSeconds seconds. No SSH fallback is exposed."
}

function Send-PZSsmCommand {
    param(
        [Parameter(Mandatory)]
        [string] $InstanceId,

        [Parameter(Mandatory)]
        [string] $Command,

        [Parameter(Mandatory)]
        [string] $Region,

        [string] $Profile,
        [int] $TimeoutSeconds = 900,
        [string] $Comment = 'pz-server management command'
    )

    $parameterPath = [System.IO.Path]::GetTempFileName()
    try {
        $parameters = @{ commands = @($Command) } | ConvertTo-Json -Compress
        [System.IO.File]::WriteAllText(
            $parameterPath,
            $parameters,
            [System.Text.UTF8Encoding]::new($false)
        )
        $parameterUri = 'file://' + $parameterPath.Replace('\', '/')
        $response = Invoke-PZAwsJson -Region $Region -Profile $Profile -Arguments @(
            'ssm', 'send-command',
            '--instance-ids', $InstanceId,
            '--document-name', 'AWS-RunShellScript',
            '--comment', $Comment,
            '--timeout-seconds', $TimeoutSeconds.ToString(),
            '--parameters', $parameterUri
        )
        return [string] $response.Command.CommandId
    }
    finally {
        Remove-Item -LiteralPath $parameterPath -Force -ErrorAction SilentlyContinue
    }
}

function Wait-PZSsmCommand {
    param(
        [Parameter(Mandatory)]
        [string] $CommandId,

        [Parameter(Mandatory)]
        [string] $InstanceId,

        [Parameter(Mandatory)]
        [string] $Region,

        [string] $Profile,
        [int] $TimeoutSeconds = 900
    )

    $deadline = [DateTimeOffset]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTimeOffset]::UtcNow -lt $deadline) {
        try {
            $invocation = Invoke-PZAwsJson -Region $Region -Profile $Profile -Arguments @(
                'ssm', 'get-command-invocation',
                '--command-id', $CommandId,
                '--instance-id', $InstanceId
            )
        }
        catch {
            Start-Sleep -Seconds 3
            continue
        }
        switch ([string] $invocation.Status) {
            'Success' { return $invocation }
            { $_ -in @('Pending', 'InProgress', 'Delayed') } {
                Start-Sleep -Seconds 5
                continue
            }
            default {
                $errorText = [string] $invocation.StandardErrorContent
                throw "SSM command $CommandId ended as $($invocation.Status): $errorText"
            }
        }
    }
    throw "SSM command $CommandId did not finish within $TimeoutSeconds seconds."
}

function Get-PZEffectiveDefault {
    param(
        [Parameter(Mandatory)]
        [string] $ParameterValue,

        [Parameter(Mandatory)]
        [string] $EnvironmentName,

        [Parameter(Mandatory)]
        [string] $Fallback,

        [Parameter(Mandatory)]
        [System.Collections.IDictionary] $BoundParameters,

        [Parameter(Mandatory)]
        [string] $ParameterName
    )

    if ($BoundParameters.ContainsKey($ParameterName)) {
        return $ParameterValue
    }
    $environmentValue = [Environment]::GetEnvironmentVariable($EnvironmentName)
    if (-not [string]::IsNullOrWhiteSpace($environmentValue)) {
        return $environmentValue
    }
    return $Fallback
}

Import-PZHelperEnvironment
