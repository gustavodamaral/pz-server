# Local Acceptance Checklist

Run this checklist before the first AWS apply and after changes to Docker, PZ configuration, RCON, shutdown, persistence, or deployment behavior. It exercises the real stable Build 42 server locally. It does not emulate EC2, EBS, SSM, systemd, or AWS billing APIs.

## Prerequisites

- Docker Desktop is running with enough memory for `.env` limits.
- `.env` exists, all `change-me-*` values were replaced, and `UPDATE_ON_START=false` after the initial download.
- A matching stable Project Zomboid client is available for player/persistence checks.
- No irreplaceable local world is used without a backup.

Build both repository images and start only the real game server:

```powershell
docker compose --profile watchdog build
docker compose up -d server
docker compose ps
docker compose logs --tail 200 server
```

Wait until `docker compose ps` reports `healthy`. Confirm logs show the intended stable branch, managed `PauseEmpty=true`, the configured `4g-8g` or local test heap, and successful server startup. Do not treat an open port alone as readiness.

Verify both memory boundaries fail before SteamCMD or Java starts:

```powershell
$xmsOutput = docker run --rm -e ADMIN_PASSWORD=test -e SERVER_PASSWORD=test -e RCON_PASSWORD=test `
  -e PZ_XMS=9g -e PZ_XMX=8g -e CONTAINER_MEMORY_LIMIT=14g pz-server:local
$xmsExit = $LASTEXITCODE
if ($xmsExit -eq 0 -or ($xmsOutput -join "`n") -notmatch 'PZ_XMS must not exceed PZ_XMX') {
  throw 'The intended XMS > XMX validation did not reject startup before installation'
}

$limitOutput = docker run --rm -e ADMIN_PASSWORD=test -e SERVER_PASSWORD=test -e RCON_PASSWORD=test `
  -e PZ_XMS=4g -e PZ_XMX=10g -e CONTAINER_MEMORY_LIMIT=10g pz-server:local
$limitExit = $LASTEXITCODE
if ($limitExit -eq 0 -or ($limitOutput -join "`n") -notmatch 'PZ_XMX must remain below CONTAINER_MEMORY_LIMIT') {
  throw 'The intended XMX limit validation did not reject startup before installation'
}
```

## Real RCON

With no client connected:

```powershell
.\scripts\windows\Test-PZRcon.ps1 -ExpectedPlayers 0
```

The helper runs the direct `rcon-cli players` command, requires the strict watchdog adapter to return `0`, then deliberately uses a wrong RCON password and requires `UNKNOWN` with exit code `2`.

Join from a real Build 42 client at `127.0.0.1:16261`, wait until the character is in-world, and run:

```powershell
.\scripts\windows\Test-PZRcon.ps1 -ExpectedPlayers 1
```

Disconnect fully, wait for the server to process the departure, and rerun the zero-player command. Record the raw response if the official server format differs; do not loosen the parser until the new response is understood and covered by tests.

## Save and Persistence

While connected, create an unmistakable world/character marker, such as moving to a known location and placing a disposable item. Disconnect, confirm zero players, then require a real save response:

```powershell
docker compose exec -T server rcon-cli --host 127.0.0.1 save
```

The complete response must be `World saved` or `World is saved`. Recreate the application without deleting bind-mounted data:

```powershell
docker compose down
docker compose up -d server
docker compose ps
.\scripts\windows\Test-PZRcon.ps1 -ExpectedPlayers 0
```

Reconnect and verify the same character, location, and marker. Never use `docker compose down --volumes` as a cleanup shortcut.

## Short Idle Dry Run

Local Compose forces `DRY_RUN=true` and disables host shutdown regardless of `.env`. Use process-level overrides so the normal file remains at 45 minutes:

```powershell
$env:IDLE_TIMEOUT_MINUTES = '2'
$env:POLL_INTERVAL_SECONDS = '10'
$env:FINAL_CHECK_DELAY_SECONDS = '2'
docker compose --profile watchdog up -d --force-recreate watchdog
docker compose logs -f watchdog
```

With zero players, observe the idle timer, all final checks, and both `[DRY RUN]` messages after roughly two minutes. The game container must remain running. Connect before timeout during a second run and confirm `Player connected; idle timer reset` with no dry-run action.

Restore normal values in the same shell and recreate the sidecar:

```powershell
Remove-Item Env:IDLE_TIMEOUT_MINUTES, Env:POLL_INTERVAL_SECONDS, Env:FINAL_CHECK_DELAY_SECONDS
docker compose --profile watchdog up -d --force-recreate watchdog
```

## Continuous RCON Failure

Stop the ordinary sidecar and run an isolated watchdog with an intentionally wrong credential:

```powershell
docker compose --profile watchdog stop watchdog
docker compose --profile watchdog run --rm --no-deps `
  -e RCON_PASSWORD=intentionally-wrong-for-acceptance `
  -e RCON_RETRY_COUNT=1 `
  -e POLL_INTERVAL_SECONDS=5 `
  watchdog run
```

Observe at least two polls, then press Ctrl+C. Every poll must report `UNKNOWN` and prohibit shutdown. Because the independent TCP health signal remains healthy, no restart is justified. Run `docker compose ps server` and require the game container to remain running, then start the normal dry-run sidecar again.

## Signal and Clean Exit

First stop the watchdog so it does not observe the intentional maintenance stop, then send Docker's normal `SIGTERM` path:

```powershell
docker compose --profile watchdog stop watchdog
$containerId = docker compose ps --quiet server
docker stop --timeout 120 $containerId
docker inspect --format '{{json .State}}' $containerId
docker compose logs --tail 200 server
```

Require every state predicate used by the production watchdog:

```powershell
$state = docker inspect --format '{{json .State}}' $containerId | ConvertFrom-Json
if ($state.Status -ne 'exited' -or $state.Running -or $state.Restarting -or $state.Dead `
    -or $state.OOMKilled -or [int]$state.ExitCode -ne 0 -or $state.Error -ne '') {
  throw 'The server did not retain an exact clean stopped state'
}
```

Logs must show the save request, quit request, and graceful completion without escalation. Restart and repeat the empty RCON smoke test:

```powershell
docker compose up -d server
docker compose --profile watchdog up -d watchdog
.\scripts\windows\Test-PZRcon.ps1 -ExpectedPlayers 0
```

## Unexpected Exit Propagation

Run this destructive check only against a disposable local world after confirming zero players and a successful save. It proves the checksum-gated Build 42 launcher wrapper does not restore upstream's masked exit code `0` behavior:

```powershell
docker compose --profile watchdog stop watchdog
$containerId = docker compose ps --quiet server
docker update --restart=no $containerId
docker exec $containerId bash -lc 'kill -KILL -- "-$(cat /tmp/pz-entrypoint.pid)"'
$containerExit = [int](docker wait $containerId)
$state = docker inspect --format '{{json .State}}' $containerId | ConvertFrom-Json
if ($containerExit -eq 0 -or [int]$state.ExitCode -eq 0 -or $state.Restarting) {
  throw 'Unexpected launcher failure was masked or independently restarted'
}
docker compose up -d server
docker compose --profile watchdog up -d watchdog
.\scripts\windows\Test-PZRcon.ps1 -ExpectedPlayers 0
```

## Repository Validation

Run the same account-independent checks as CI:

```powershell
.\.venv\Scripts\ruff check .
.\.venv\Scripts\ruff format --check .
.\.venv\Scripts\pytest
docker compose config --quiet
docker compose --profile watchdog build
terraform -chdir=terraform fmt -check -recursive
terraform -chdir=terraform init -backend=false -input=false
terraform -chdir=terraform validate
```

Also run ShellCheck on all `*.sh` files and parse every `scripts/windows/*.ps1` file. These checks intentionally do not run `terraform plan` against an account, `terraform apply`, SSM, systemd, EBS attach/resize, EC2 protection/replacement, or AWS Budget delivery. Validate those only in a reviewed AWS maintenance window.
