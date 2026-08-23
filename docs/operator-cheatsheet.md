# Project Zomboid Production Operator Cheatsheet

Practical commands for operating the AWS Project Zomboid Build 42 server.

This server is intentionally managed through the repository helpers, `pzctl`, systemd, SSM, and RCON. Prefer the guarded commands here over raw Docker or EC2 lifecycle commands.

## 1. Windows / AWS login

Open PowerShell in the repository:

```powershell
cd C:\Projetos\pz-server
$env:AWS_PROFILE = "pz-admin"
```

If AWS SSO expired:

```powershell
aws sso login --profile pz-admin
```

Optional identity check:

```powershell
aws sts get-caller-identity --profile pz-admin
```

## 2. Start the server

Normal mode:

```powershell
.\scripts\windows\Start-PZ.ps1 -Profile pz-admin
```

Party mode / larger EC2 type:

```powershell
.\scripts\windows\Start-PZ.ps1 -Mode Party -Profile pz-admin
```

The helper waits for EC2, SSM, PZ health, exact RCON readiness, updater state, and watchdog readiness before printing the current public IP.

The public IPv4 is normally different after an EC2 stop/start unless an Elastic IP is configured.

## 3. Stop the server safely

Preferred Windows command:

```powershell
.\scripts\windows\Stop-PZ.ps1 -Profile pz-admin
```

If players are still connected and everybody has explicitly agreed to maintenance:

```powershell
.\scripts\windows\Stop-PZ.ps1 -Profile pz-admin -AllowConnectedPlayers
```

Do **not** use `-Force` during normal operation. It bypasses the PZ save/quit path and is only an emergency fallback when graceful management is unavailable.

The watchdog also performs an automatic guarded shutdown after the configured idle period when it can repeatedly confirm zero players.

## 4. Connect to the EC2 host with SSM

```powershell
cd C:\Projetos\pz-server
$env:AWS_PROFILE = "pz-admin"
$instanceId = terraform -chdir=terraform output -raw instance_id

aws ssm start-session `
  --region us-east-1 `
  --target $instanceId
```

If the SSO token expired, run `aws sso login --profile pz-admin` first.

## 5. Get the current public IP

Normally just use the IP printed by `Start-PZ.ps1`.

To query it manually from PowerShell:

```powershell
$instanceId = terraform -chdir=terraform output -raw instance_id

aws ec2 describe-instances `
  --profile pz-admin `
  --region us-east-1 `
  --instance-ids $instanceId `
  --query "Reservations[0].Instances[0].PublicIpAddress" `
  --output text
```

A stopped instance without an Elastic IP does not retain its previous public IPv4.

## 6. Main `pzctl` commands on the EC2 host

`pzctl` requires root, so use `sudo`.

### Overall server status

```bash
sudo pzctl status
```

Machine-readable status:

```bash
sudo pzctl status --json
```

The status includes player count, CPU, hottest CPU core, PZ memory, system memory, uptime, disk use, Steam build, update state, and PZ version.

### Player count

```bash
sudo pzctl players
```

### Save the world now

```bash
sudo pzctl save
```

### Wait for full readiness

```bash
sudo pzctl wait-ready --timeout 120
```

### Start PZ + watchdog

```bash
sudo pzctl start
```

### Gracefully stop PZ without powering off EC2

```bash
sudo pzctl graceful-stop
```

Explicitly allow connected players only for announced maintenance:

```bash
sudo pzctl graceful-stop --allow-connected-players
```

### Graceful restart

```bash
sudo pzctl restart
```

### Gracefully save, stop PZ, and stop the EC2 host

```bash
sudo pzctl shutdown-host
```

Explicit maintenance override:

```bash
sudo pzctl shutdown-host --allow-connected-players
```

### Follow PZ logs

```bash
sudo pzctl logs
```

Press `Ctrl+C` to stop following the logs; this does not stop the server.

## 7. Diagnose lag / desync while it is happening

Run these **while the lag is happening**, not several minutes later.

### PZ + host metrics

```bash
sudo pzctl status
```

Important values:

- `Hottest core`: sustained values close to 100% suggest CPU pressure even when total CPU looks moderate.
- `PZ process`: Project Zomboid JVM/native memory use.
- `Available`: free/available host memory.
- `Players`: confirms the server still has an authoritative RCON view of connected players.

For repeated snapshots during a problem:

```bash
sudo watch -n 5 pzctl status
```

Press `Ctrl+C` to exit `watch`.

### Docker CPU / memory snapshot

```bash
sudo docker stats --no-stream
```

### Linux memory

```bash
free -h
```

### Linux load

```bash
uptime
```

### Recent PZ stack service messages

```bash
sudo journalctl -u pz-stack.service -n 100 --no-pager
```

### Recent watchdog messages

```bash
sudo journalctl -u pz-watchdog.service -n 100 --no-pager
```

### Follow watchdog live

```bash
sudo journalctl -u pz-watchdog.service -f
```

## 8. systemd service health

PZ stack:

```bash
sudo systemctl status pz-stack.service --no-pager -l
```

Watchdog:

```bash
sudo systemctl status pz-watchdog.service --no-pager -l
```

Docker:

```bash
sudo systemctl status docker.service --no-pager -l
```

Avoid using raw `systemctl restart pz-stack.service` as the normal lifecycle workflow. Prefer `pzctl` so the project safety checks and lifecycle lock remain in control.

## 9. Docker / Compose inspection

List the Compose service/container state:

```bash
sudo docker compose \
  --project-directory /opt/pz-stack \
  --file /opt/pz-stack/compose.yaml \
  --env-file /srv/pz/secrets.env \
  ps
```

Inspect all containers if troubleshooting:

```bash
sudo docker ps -a
```

Prefer `sudo pzctl logs` for normal PZ log viewing.

## 10. RCON commands without logging into the game as admin

Do **not** run:

```bash
source /srv/pz/secrets.env
```

`secrets.env` is valid Compose dotenv syntax, but values such as the public server name or map name may contain spaces and are not guaranteed to be valid shell assignments.

For an SSM shell session, define this temporary helper function:

```bash
pz-rcon() {
  sudo bash -c '
    RCON_PASSWORD="$(sed -n "s/^RCON_PASSWORD=//p" /srv/pz/secrets.env)"
    export RCON_PASSWORD
    docker compose \
      --project-directory /opt/pz-stack \
      --file /opt/pz-stack/compose.yaml \
      --env-file /srv/pz/secrets.env \
      exec -T -e RCON_PASSWORD server \
      rcon-cli --host 127.0.0.1 --port 27015 "$@"
  ' bash "$@"
}
```

This function exists only in the current shell session.

### List connected players through RCON

```bash
pz-rcon players
```

### Teleport one player to another

```bash
pz-rcon teleportplayer PLAYER_TO_MOVE DESTINATION_PLAYER
```

Example:

```bash
pz-rcon teleportplayer Alex franco
```

That moves `Alex` to `franco`.

### Save through raw RCON

Normally prefer `sudo pzctl save`, but for RCON testing:

```bash
pz-rcon save
```

## 11. Passwords / secrets

Production secrets live on the persistent volume at:

```text
/srv/pz/secrets.env
```

Do not dump the whole file to the terminal unless there is a specific recovery reason.

Show only the server join password:

```bash
sudo grep '^SERVER_PASSWORD=' /srv/pz/secrets.env
```

Show only the configured bootstrap admin password:

```bash
sudo grep '^ADMIN_PASSWORD=' /srv/pz/secrets.env
```

Do not print `RCON_PASSWORD` unless absolutely necessary; normal operations can use it without displaying it.

## 12. Backup

Create a guarded world backup:

```bash
sudo pz-backup
```

By default it refuses to proceed with connected players.

Explicit announced-maintenance override:

```bash
sudo pz-backup --allow-connected-players
```

List backups:

```bash
sudo ls -lh /srv/pz/backups
```

The backup script stops PZ cleanly when necessary, creates the archive and checksum, and restarts PZ if it was running.

## 13. Game update

Normal stable-on-start policy checks for a new stable PZ build when starting a gameplay session.

Manual update operation:

```bash
sudo pzctl update
```

Explicit Steam validation / repair operation:

```bash
sudo pzctl update --validate
```

Do not use `--validate` as the routine startup path; it is intended as a repair operation.

## 14. Repository application deployment

From Windows, after a reviewed commit is pushed:

```powershell
cd C:\Projetos\pz-server
$commit = git rev-parse HEAD
.\scripts\windows\Deploy-PZ.ps1 -CommitSha $commit
```

The deployment helper uses an exact commit SHA and performs the guarded stop/deploy/readiness/rollback transaction.

Check the repository checkout currently installed on the host:

```bash
git -C /opt/pz-stack rev-parse HEAD
```

Check the deployment marker if present:

```bash
sudo cat /var/lib/pz-deploy/deployed-commit
```

## 15. Persistent storage checks

Mounted world/data filesystem:

```bash
findmnt /srv/pz
```

Disk usage:

```bash
df -h /srv/pz
```

Block devices / filesystems:

```bash
lsblk -f
```

The persistent world/data EBS volume is separate from the disposable EC2 root volume.

## 16. Useful AWS instance state check

From PowerShell:

```powershell
$instanceId = terraform -chdir=terraform output -raw instance_id

aws ec2 describe-instances `
  --profile pz-admin `
  --region us-east-1 `
  --instance-ids $instanceId `
  --query "Reservations[0].Instances[0].{State:State.Name,Type:InstanceType,IP:PublicIpAddress}" `
  --output table
```

## 17. Emergency rules

- Prefer `pzctl` over raw Docker stop/restart commands.
- Prefer `Stop-PZ.ps1` or `pzctl shutdown-host` over stopping EC2 directly.
- Do not terminate the EC2 instance as a routine stop operation.
- Do not force-stop EC2 unless the graceful path is genuinely unavailable.
- Do not `source /srv/pz/secrets.env`.
- Do not expose the full secrets file in screenshots, chat, logs, or shell history.
- If RCON/player state is `UNKNOWN`, investigate instead of assuming zero players.
- Capture performance metrics while lag/desync is actually occurring.
