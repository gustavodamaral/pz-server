[CmdletBinding()]
param(
    [string] $Destination,
    [switch] $AllowConnectedPlayers,
    [ValidateRange(1, 365)]
    [int] $RetentionCount = 7
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Invoke-Docker {
    param([Parameter(Mandatory)][string[]] $Arguments)
    $output = @(& docker @Arguments 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw "Docker command failed: $($output -join [Environment]::NewLine)"
    }
    return $output -join [Environment]::NewLine
}

function Get-ConfirmedPlayerCount {
    param([Parameter(Mandatory)][string] $Response)
    $clean = [regex]::Replace($Response, '\x1B\[[0-?]*[ -/]*[@-~]', '')
    $lines = @($clean -split '\r?\n' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    if ($lines.Count -eq 0) { throw 'Player response was empty.' }
    $match = [regex]::Match(
        $lines[0],
        '^Players\s+connected\s*(?:\(\s*(\d+)\s*\)\s*:?\s*|:\s*(\d+)\s*)$',
        [Text.RegularExpressions.RegexOptions]::IgnoreCase
    )
    if (-not $match.Success) { throw 'Player response header was not recognized.' }
    foreach ($line in @($lines | Select-Object -Skip 1)) {
        if (-not $line.StartsWith('-')) { throw 'Player response contained unexpected output.' }
    }
    $count = if ($match.Groups[1].Success) {
        [int] $match.Groups[1].Value
    } else {
        [int] $match.Groups[2].Value
    }
    if (($lines.Count - 1) -ne $count) { throw 'Player response count did not match its entries.' }
    return $count
}

function Assert-ConfirmedSave {
    param([Parameter(Mandatory)][string] $Response)
    $clean = [regex]::Replace($Response, '\x1B\[[0-?]*[ -/]*[@-~]', '').Trim()
    if ($clean -notmatch '(?i)^World\s+(?:is\s+)?saved$') {
        throw 'RCON save did not return an exact recognized confirmation.'
    }
}

function Get-ComposeContainerId {
    $output = (Invoke-Docker @('compose', 'ps', '--all', '--quiet', 'server')).Trim()
    $identifiers = @($output -split '\r?\n' | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    if ($identifiers.Count -gt 1) { throw 'Compose returned multiple server containers.' }
    if ($identifiers.Count -eq 0) { return $null }
    return $identifiers[0]
}

function Assert-ContainerQuiescent {
    param([AllowNull()][string] $ExpectedIdentifier)
    $currentIdentifier = Get-ComposeContainerId
    if ($currentIdentifier -ne $ExpectedIdentifier) {
        throw 'Server container identity changed during backup.'
    }
    if (-not $ExpectedIdentifier) { return }
    $state = Invoke-Docker @('inspect', '--format', '{{json .State}}', $ExpectedIdentifier) | ConvertFrom-Json
    if ($state.Running -or $state.Restarting -or $state.OOMKilled -or [int] $state.ExitCode -ne 0) {
        throw 'The exact server container is not in a verified clean stopped state.'
    }
}

$repositoryRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$composeFile = Join-Path $repositoryRoot 'compose.yaml'
$composeConfiguration = Invoke-Docker @(
    'compose', '--project-directory', $repositoryRoot, '--file', $composeFile,
    'config', '--format', 'json'
) | ConvertFrom-Json
$dataVolumes = @($composeConfiguration.services.server.volumes) | Where-Object {
    $_.target -eq '/home/pz/Zomboid' -and $_.type -eq 'bind'
}
if (@($dataVolumes).Count -ne 1) {
    throw 'Compose must render exactly one bind mount for /home/pz/Zomboid.'
}
$dataPath = [IO.Path]::GetFullPath([string] $dataVolumes[0].source)
if (-not (Test-Path -LiteralPath $dataPath -PathType Container)) {
    throw "Persistent data directory does not exist: $dataPath"
}
if (-not $Destination) {
    $Destination = Join-Path $repositoryRoot 'backups'
}
if (-not (Test-Path -LiteralPath $Destination)) {
    $null = New-Item -ItemType Directory -Path $Destination
}
$Destination = [IO.Path]::GetFullPath($Destination)
$dataPrefix = $dataPath.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
if ($Destination.Equals($dataPath, [StringComparison]::OrdinalIgnoreCase) -or
    $Destination.StartsWith($dataPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Backup destination must be outside the persistent data directory being archived.'
}
if (-not (Get-Command -Name 'tar.exe' -ErrorAction SilentlyContinue)) {
    throw 'tar.exe was not found on PATH.'
}

$backupMutex = [Threading.Mutex]::new($false, 'Local\PZServerBackup')
$lockTaken = $backupMutex.WaitOne(0)
if (-not $lockTaken) {
    $backupMutex.Dispose()
    throw 'Another local Project Zomboid backup is already running.'
}
$wasRunning = $false
$locationPushed = $false
$partialArchive = $null
$finalArchive = $null
$backupComplete = $false
$restartFailed = $false
try {
    Push-Location $repositoryRoot
    $locationPushed = $true
    $containerId = Get-ComposeContainerId
    if ($containerId) {
        $initialState = Invoke-Docker @('inspect', '--format', '{{json .State}}', $containerId) | ConvertFrom-Json
        if ($initialState.Restarting) { throw 'Server container is restarting; refusing backup.' }
        $wasRunning = [bool] $initialState.Running
        if (-not $wasRunning) { Assert-ContainerQuiescent $containerId }
    }
    if ($wasRunning) {
        $playersOutput = Invoke-Docker @('compose', 'exec', '-T', 'server', 'rcon-cli', '--host', '127.0.0.1', 'players')
        $playerCount = Get-ConfirmedPlayerCount $playersOutput
        if ($playerCount -gt 0 -and -not $AllowConnectedPlayers) {
            throw "Refusing backup while $playerCount player(s) are connected."
        }
        $saveOutput = Invoke-Docker @('compose', 'exec', '-T', 'server', 'rcon-cli', '--host', '127.0.0.1', 'save')
        Assert-ConfirmedSave $saveOutput
        Start-Sleep -Seconds 10
        $playersOutput = Invoke-Docker @('compose', 'exec', '-T', 'server', 'rcon-cli', '--host', '127.0.0.1', 'players')
        $playerCount = Get-ConfirmedPlayerCount $playersOutput
        if ($playerCount -gt 0 -and -not $AllowConnectedPlayers) {
            throw 'A player connected during save; refusing backup.'
        }
        $null = Invoke-Docker @('compose', 'stop', '--timeout', '120', 'server')
        Assert-ContainerQuiescent $containerId
    }

    $timestamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ')
    $finalArchive = Join-Path $Destination "pz-world-$timestamp.tar.gz"
    $partialArchive = Join-Path $Destination ".$([IO.Path]::GetFileName($finalArchive)).$PID.partial"
    if (Test-Path -LiteralPath $finalArchive) { throw "Backup filename collision: $finalArchive" }
    $tarOutput = @(& tar.exe -czf $partialArchive -C $dataPath . 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw "tar.exe failed: $($tarOutput -join [Environment]::NewLine)"
    }
    Assert-ContainerQuiescent $containerId
    Move-Item -LiteralPath $partialArchive -Destination $finalArchive
    $partialArchive = $null
    $hash = Get-FileHash -Algorithm SHA256 -LiteralPath $finalArchive
    [IO.File]::WriteAllText("$finalArchive.sha256", "$($hash.Hash.ToLowerInvariant())  $([IO.Path]::GetFileName($finalArchive))`n")
    $expiredArchives = @(Get-ChildItem -LiteralPath $Destination -Filter 'pz-world-*.tar.gz' -File |
        Sort-Object -Property LastWriteTimeUtc -Descending |
        Select-Object -Skip $RetentionCount)
    foreach ($expiredArchive in $expiredArchives) {
        Remove-Item -LiteralPath $expiredArchive.FullName -Force
        Remove-Item -LiteralPath "$($expiredArchive.FullName).sha256" -Force -ErrorAction SilentlyContinue
    }
    $backupComplete = $true
    Write-Host "Backup created: $finalArchive"
    Write-Host "Retention: newest $RetentionCount local archive(s)"
    Write-Host 'Keep an additional copy outside this machine.'
}
finally {
    if ($partialArchive -and (Test-Path -LiteralPath $partialArchive)) {
        Remove-Item -LiteralPath $partialArchive -Force -ErrorAction SilentlyContinue
    }
    if (-not $backupComplete -and $finalArchive) {
        Remove-Item -LiteralPath $finalArchive -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath "$finalArchive.sha256" -Force -ErrorAction SilentlyContinue
    }
    if ($wasRunning) {
        try {
            $null = Invoke-Docker @('compose', 'up', '-d', 'server')
        }
        catch {
            $restartFailed = $true
            Write-Warning "Backup cleanup could not restart the server: $($_.Exception.Message)"
        }
    }
    if ($locationPushed) { Pop-Location }
    if ($lockTaken) { $backupMutex.ReleaseMutex() }
    $backupMutex.Dispose()
    if ($restartFailed) { throw 'Backup finished, but the previous running state could not be restored.' }
}
