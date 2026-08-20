[CmdletBinding()]
param(
    [ValidateRange(0, 100)]
    [int] $ExpectedPlayers = 0
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repositoryRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$composeFile = Join-Path $repositoryRoot 'compose.yaml'
$environmentFile = Join-Path $repositoryRoot '.env'

function Invoke-PZComposeCapture {
    param(
        [Parameter(Mandatory)]
        [string[]] $Arguments
    )

    $baseArguments = @(
        'compose',
        '--project-directory', $repositoryRoot,
        '--file', $composeFile,
        '--env-file', $environmentFile
    )
    $stdoutPath = [System.IO.Path]::GetTempFileName()
    $stderrPath = [System.IO.Path]::GetTempFileName()
    $previousErrorActionPreference = $ErrorActionPreference
    $nativePreference = Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue
    $previousNativePreference = if ($null -eq $nativePreference) { $null } else { $nativePreference.Value }
    try {
        $ErrorActionPreference = 'Continue'
        if ($null -ne $nativePreference) {
            Set-Variable -Name PSNativeCommandUseErrorActionPreference -Value $false
        }
        & docker @baseArguments @Arguments 1> $stdoutPath 2> $stderrPath
        $exitCode = $LASTEXITCODE
        $stdoutLines = @([System.IO.File]::ReadAllLines($stdoutPath) | ForEach-Object { $_.TrimEnd() })
        $stderrLines = @([System.IO.File]::ReadAllLines($stderrPath) | ForEach-Object { $_.TrimEnd() })
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
        if ($null -ne $nativePreference) {
            Set-Variable -Name PSNativeCommandUseErrorActionPreference -Value $previousNativePreference
        }
        Remove-Item -LiteralPath $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue
    }
    return [pscustomobject]@{
        ExitCode = $exitCode
        StdoutLines = $stdoutLines
        StderrLines = $stderrLines
        Lines = @($stdoutLines + $stderrLines)
    }
}

function Get-PZRawPlayerCount {
    param(
        [Parameter(Mandatory)]
        [string[]] $Lines
    )

    $normalized = @(
        foreach ($line in $Lines) {
            $clean = ($line -replace "$([char] 27)\[[0-?]*[ -/]*[@-~]", '').Trim()
            if ($clean) { $clean }
        }
    )
    if ($normalized.Count -eq 0) {
        throw 'Raw RCON returned no output.'
    }
    $count = $null
    if ($normalized[0] -match '^Players\s+connected\s*\(\s*(\d+)\s*\)\s*:?\s*$') {
        $count = [int] $Matches[1]
    }
    elseif ($normalized[0] -match '^Players\s+connected\s*:\s*(\d+)\s*$') {
        $count = [int] $Matches[1]
    }
    if ($null -eq $count) {
        throw 'Raw RCON first line was not a recognized player header.'
    }
    $playerLines = @()
    if ($normalized.Count -gt 1) {
        $playerLines = @($normalized[1..($normalized.Count - 1)])
    }
    if ($playerLines.Count -ne $count -or @($playerLines | Where-Object { $_ -notmatch '^-' }).Count -ne 0) {
        throw "Raw RCON declared $count player(s), but the complete response did not contain exactly that many player lines."
    }
    return $count
}

if (-not (Get-Command -Name 'docker' -ErrorAction SilentlyContinue)) {
    throw 'Docker CLI was not found on PATH.'
}
if (-not (Test-Path -LiteralPath $environmentFile -PathType Leaf)) {
    throw "Missing $environmentFile. Create the local environment before running the smoke test."
}

$running = Invoke-PZComposeCapture -Arguments @('ps', '--status', 'running', '--quiet', 'server')
if ($running.ExitCode -ne 0) {
    throw "Could not inspect the server container: $($running.Lines -join [Environment]::NewLine)"
}
$containerIds = @($running.Lines | Where-Object { $_ -match '^[0-9a-f]{12,64}$' })
if ($containerIds.Count -ne 1) {
    throw "Expected exactly one running Compose server container, found $($containerIds.Count)."
}

$raw = Invoke-PZComposeCapture -Arguments @(
    'exec', '-T', 'server',
    'rcon-cli', '--host', '127.0.0.1', 'players'
)
Write-Host 'Direct Build 42 RCON response:'
$raw.StdoutLines | ForEach-Object { Write-Host $_ }
if ($raw.ExitCode -ne 0 -or $raw.StderrLines.Count -ne 0) {
    throw "Direct RCON command failed or wrote stderr: $($raw.Lines -join [Environment]::NewLine)"
}
$rawCount = Get-PZRawPlayerCount -Lines $raw.StdoutLines
if ($rawCount -ne $ExpectedPlayers) {
    throw "Direct RCON reported $rawCount player(s); expected $ExpectedPlayers."
}

$adapter = Invoke-PZComposeCapture -Arguments @(
    '--profile', 'watchdog',
    'run', '--rm', '--no-deps',
    'watchdog', 'players'
)
$adapterOutput = @($adapter.StdoutLines | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
if ($adapter.ExitCode -ne 0 -or $adapter.StderrLines.Count -ne 0 -or $adapterOutput.Count -ne 1 -or $adapterOutput[0] -notmatch '^\d+$') {
    throw "Watchdog adapter did not return one confirmed count: $($adapter.Lines -join [Environment]::NewLine)"
}
if ([int] $adapterOutput[0] -ne $ExpectedPlayers) {
    throw "Watchdog adapter reported $($adapterOutput[0]) player(s); expected $ExpectedPlayers."
}

$wrongPassword = "intentionally-wrong-$([Guid]::NewGuid().ToString('N'))"
$unavailable = Invoke-PZComposeCapture -Arguments @(
    '--profile', 'watchdog',
    'run', '--rm', '--no-deps',
    '-e', "RCON_PASSWORD=$wrongPassword",
    '-e', 'RCON_RETRY_COUNT=1',
    '-e', 'RCON_RETRY_DELAY_SECONDS=1',
    '-e', 'RCON_TIMEOUT_SECONDS=3',
    'watchdog', 'players'
)
$unavailableOutput = @($unavailable.StdoutLines | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
if ($unavailable.ExitCode -ne 2 -or $unavailableOutput.Count -ne 1 -or $unavailableOutput[0] -ne 'UNKNOWN') {
    throw "Unavailable RCON did not fail closed as UNKNOWN/exit 2: $($unavailable.Lines -join [Environment]::NewLine)"
}

Write-Host ''
Write-Host "RCON smoke test passed: direct=$rawCount, adapter=$($adapterOutput[0]), unavailable=UNKNOWN."
