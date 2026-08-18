[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string] $Archive,
    [switch] $Start
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

$repositoryRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$archivePath = [IO.Path]::GetFullPath($Archive)
if (-not (Test-Path -LiteralPath $archivePath -PathType Leaf)) {
    throw "Backup archive does not exist: $archivePath"
}
$checksumPath = "$archivePath.sha256"
if (-not (Test-Path -LiteralPath $checksumPath -PathType Leaf)) {
    throw "Backup checksum does not exist: $checksumPath"
}
$checksumLine = [IO.File]::ReadAllText($checksumPath).Trim()
if ($checksumLine -notmatch '^([0-9a-fA-F]{64})\s+') {
    throw 'Backup checksum file is malformed.'
}
$expectedHash = $Matches[1]
$actualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $archivePath).Hash
if ($actualHash -ne $expectedHash) {
    throw 'Backup checksum verification failed.'
}
if (-not (Get-Command -Name 'tar.exe' -ErrorAction SilentlyContinue)) {
    throw 'tar.exe was not found on PATH.'
}

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
$backupMutex = [Threading.Mutex]::new($false, 'Local\PZServerBackup')
$lockTaken = $backupMutex.WaitOne(0)
if (-not $lockTaken) {
    $backupMutex.Dispose()
    throw 'A local Project Zomboid backup or restore is already running.'
}
$parentPath = Split-Path -Parent $dataPath
$timestamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ')
$extractionPath = "$dataPath.restore-$timestamp"
$previousPath = "$dataPath.before-restore-$timestamp"
$locationPushed = $false
$extracted = $false

try {
    Push-Location $repositoryRoot
    $locationPushed = $true
    $containerOutput = (Invoke-Docker @('compose', 'ps', '--all', '--quiet', 'server')).Trim()
    $containerIds = @($containerOutput -split '\r?\n' | Where-Object { $_ })
    if ($containerIds.Count -gt 1) { throw 'Compose returned multiple server containers.' }
    if ($containerIds.Count -eq 1) {
        $state = Invoke-Docker @('inspect', '--format', '{{json .State}}', $containerIds[0]) | ConvertFrom-Json
        if ($state.Running -or $state.Restarting) {
            throw 'The Project Zomboid container is running or restarting. Stop it cleanly before restore.'
        }
    }
    $null = Invoke-Docker @('compose', 'down')

    if (-not (Test-Path -LiteralPath $parentPath -PathType Container)) {
        $null = New-Item -ItemType Directory -Path $parentPath
    }
    if (Test-Path -LiteralPath $extractionPath) {
        throw "Temporary restore path already exists: $extractionPath"
    }
    $null = New-Item -ItemType Directory -Path $extractionPath
    $extracted = $true
    $tarOutput = @(& tar.exe -xzf $archivePath -C $extractionPath 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw "tar.exe failed: $($tarOutput -join [Environment]::NewLine)"
    }
    if (Test-Path -LiteralPath $dataPath) {
        if (Test-Path -LiteralPath $previousPath) {
            throw "Restore safety path already exists: $previousPath"
        }
        Move-Item -LiteralPath $dataPath -Destination $previousPath
    }
    try {
        Move-Item -LiteralPath $extractionPath -Destination $dataPath
        $extracted = $false
    }
    catch {
        if ((Test-Path -LiteralPath $previousPath) -and -not (Test-Path -LiteralPath $dataPath)) {
            Move-Item -LiteralPath $previousPath -Destination $dataPath
        }
        throw
    }

    Write-Host "Restore completed from: $archivePath"
    if (Test-Path -LiteralPath $previousPath) {
        Write-Host "Previous data retained at: $previousPath"
    }
    if ($Start) {
        $null = Invoke-Docker @('compose', 'up', '-d', 'server')
        Write-Host 'Project Zomboid start requested; inspect logs and test identity before deleting old data.'
    }
    else {
        Write-Host 'Server remains stopped. Start only after inspecting the restored files.'
    }
}
finally {
    if ($extracted -and (Test-Path -LiteralPath $extractionPath)) {
        Remove-Item -LiteralPath $extractionPath -Recurse -Force -ErrorAction SilentlyContinue
    }
    if ($locationPushed) { Pop-Location }
    if ($lockTaken) { $backupMutex.ReleaseMutex() }
    $backupMutex.Dispose()
}
