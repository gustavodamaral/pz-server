# Troubleshooting

## SteamCMD Download or Update Fails

- Inspect `docker compose logs server` for Valve connectivity, disk-full, or branch errors.
- Confirm App ID remains `380870` in official PZ guidance.
- An empty `STEAM_BRANCH` means stable. Do not invent `stable` as a beta branch; the entrypoint treats it as default.
- Retry after checking Steam service status and local DNS/proxy/firewall.
- Set `STEAM_VALIDATE=true` for one controlled repair start, then reset it.
- Never delete `runtime/zomboid` to fix a binary download.

## Container Is Unhealthy

Docker health requires the Java GameServer process and is deliberately independent of RCON. The readiness command separately requires both process health and a valid local RCON response. During first download/start, container health remains `starting` for up to 15 minutes before process failures count.

```powershell
docker compose ps
docker compose logs --tail 300 server
docker compose exec server ps -ef
docker compose exec server pz-healthcheck
```

Check bad passwords, port collisions, JVM allocation versus Docker memory, stale Build 41 data, and full disks. The watchdog does not cause Docker itself to restart merely because health is `unhealthy`; production recovery waits for the conservative outage threshold.

## Server Is Not Visible or Reachable

- Direct-connect to the current IP and port `16261`; `Public=false` intentionally avoids the public browser.
- Local clients use `127.0.0.1`.
- Verify both `16261/udp` and `16262/udp`; TCP tests do not prove UDP reachability.
- Check Windows firewall/Docker Desktop networking locally.
- Check the AWS security group and `allowed_game_cidrs` in production.
- After EC2 stop/start, use `Start-PZ.ps1` output; the old public IP is normally obsolete.
- RCON `27015/tcp` is intentionally unreachable from another computer.

## RCON Fails or Watchdog Stays UNKNOWN

```powershell
docker compose exec -T server rcon-cli --host 127.0.0.1 players
docker compose exec -T server pz-healthcheck
```

Confirm `.env` and the runtime INI agree on `RCON_PASSWORD`/`RCONPort`. The managed merge applies changes at container start, not inside an already running JVM. Unexpected `players` output deliberately becomes `UNKNOWN`; compare it with `src/pz_watchdog/rcon.py` and current supported PZ output before updating parser tests.

Do not work around UNKNOWN by reading the process list as zero players.

## Permissions or Persistent Data Ownership

The container runs UID/GID 1000. Linux bind paths must be writable:

```bash
sudo chown -R 1000:1000 runtime/server runtime/zomboid
```

On AWS, `/srv/pz/data` and `/var/lib/pz-server` should be `1000:1000`; `/srv/pz/secrets.env` must remain `root:root 0600`. Do not recursively chown the entire `/srv/pz` tree because that would expose secrets.

## PZ Exits on JVM Memory

Ensure `PZ_XMS <= PZ_XMX < CONTAINER_MEMORY_LIMIT` with room for native/off-heap memory. For an 8 GiB limit use a 6 GiB maximum, not 8 GiB. Docker Desktop's WSL VM also needs enough memory. Review OOM events with `docker inspect` and Docker Desktop diagnostics.

If upstream changes `ProjectZomboid64.json` memory flags, the entrypoint intentionally refuses to patch it. Review the new official launcher configuration and update the validated replacement logic rather than bypassing the check.

## Graceful Stop Hangs

Look for the timestamped entrypoint lines requesting `save` and `quit`. `docker stop` allows 120 seconds. A PZ console hang eventually escalates, exits the container nonzero, and fails exact-container verification; create/verify a backup before the next session. Manual AWS stop refuses an RCON save failure or unknown player state. `-AllowConnectedPlayers` bypasses only a known nonzero count. `-Force` is a separate last-resort EC2 hard stop and bypasses the save path entirely.

## EC2 Starts but PZ Does Not

Use Session Manager, not SSH:

```bash
sudo cloud-init status --long
sudo journalctl -u cloud-final -b --no-pager
sudo systemctl status pz-stack pz-watchdog
sudo journalctl -u pz-stack -b --no-pager
sudo docker compose --project-directory /opt/pz-stack --env-file /srv/pz/secrets.env ps
```

If `/opt/pz-stack` is absent, verify the Git URL is public and the exact configured commit SHA was pushed. If `/srv/pz` is not mounted, compare `lsblk -o NAME,SERIAL,FSTYPE,MOUNTPOINTS` with Terraform's persistent volume ID. `No filesystem was detected` is intentionally fatal unless this is the first blank volume and one-time initialization was explicitly armed. Never enable formatting or run `mkfs` to repair an existing/damaged volume; snapshot it and diagnose first.

Bootstrap and stack-readiness failures intentionally leave EC2 online because player/save state is unknown. Diagnose through SSM, inspect Docker state, and stop through `Stop-PZ.ps1` only when its guarded path succeeds. If cloud-init's once-only command failed, rerun `/usr/local/sbin/pz-first-boot` only after correcting the cause, or replace the disposable instance through a reviewed Terraform plan. Use a Billing alarm to detect forgotten failed hosts.

## SSM Is Offline

- Confirm EC2 has passed status checks and outbound HTTPS/DNS works.
- Confirm the instance profile and `AmazonSSMManagedInstanceCore` attachment.
- Check `systemctl status amazon-ssm-agent` or the snap unit from the EC2 serial console if available.
- Confirm account/region in AWS CLI.
- There is intentionally no public SSH fallback. `Stop-PZ.ps1` fails safely; `-Force` is a clearly destructive last resort.

## Watchdog Restarts PZ but Never Stops EC2

This is correct during management failure: restart is the first response, never host shutdown. For idle shutdown, inspect:

```bash
sudo journalctl -u pz-watchdog -b --no-pager
sudo grep -E '^(DRY_RUN|DEPLOYMENT_ENVIRONMENT|HOST_SHUTDOWN_ENABLED)=' /srv/pz/secrets.env
sudo cat /etc/pz-server/allow-host-shutdown
```

The exact guard content is `PZ_HOST_SHUTDOWN=ENABLED`. Do not arm it on a local Windows host. Every UNKNOWN resets the 45-minute timer, so recurring RCON failures correctly prevent shutdown and may increase compute cost until repaired.

## EBS Grew but Filesystem Did Not

Never reduce EBS or ext4 size. After increasing `data_volume_size_gib` and applying, confirm no players, create a backup, and run:

```bash
sudo systemctl restart pz-stack.service
findmnt /srv/pz
df -h /srv/pz
```

The pre-start resize hook is idempotent. If it fails, inspect `lsblk`, `findmnt`, and `journalctl -u pz-stack`; do not run filesystem tools against an unverified device.

## World or Player Identity Appears Reset

Stop immediately and preserve the current data directory. Verify `PZ_SERVER_NAME`, `runtime/zomboid/Server`, `db`, and `Saves/Multiplayer` all refer to the same server name. Restore a known backup if `ResetID`, `ServerPlayerID`, or database/save paths changed. The managed INI merge is specifically designed not to overwrite generated IDs; avoid replacing the runtime INI wholesale.
