# Architecture

## Boundaries

Terraform owns durable infrastructure, not PZ installation details. Its cloud-init payload installs Docker, verifies and mounts the independently created EBS volume, checks out an exact Git commit, and calls `scripts/linux/install-host.sh`. Existing instance user data is deliberately ignored after creation; exact-SHA routine deployments use the guarded SSM/`pzctl deploy` path instead. Application behavior remains reviewable and testable outside HCL.

Docker owns the PZ runtime. The custom Debian image contains Valve SteamCMD, required 32-bit compatibility libraries, checksum-pinned `rcon-cli`, and lifecycle scripts. It does not contain PZ binaries or secrets. PZ binaries are downloaded into `/opt/pzserver`; all user data is under `/home/pz/Zomboid`.

Host automation owns privileged lifecycle effects. The Python watchdog runs under systemd on AWS, accesses RCON through host loopback, inspects Docker health, and controls Compose. Neither application container receives the Docker socket.

## Persistent and Disposable State

The root EBS volume is encrypted and disposable. It holds Ubuntu, Docker, the Git checkout, the Python virtual environment, and reacquirable Steam server files. The independent encrypted gp3 volume holds `/srv/pz/data`, `/srv/pz/backups`, and `/srv/pz/secrets.env`.

This split makes EC2 replacement recoverable: attach the same volume in its fixed availability zone, clone the application, regenerate host tooling, and start against the existing world and identity database.

## Boot Sequence

1. EC2 boots the latest selected Canonical Ubuntu 24.04 AMI with IMDSv2 required.
2. SSM Agent establishes outbound management connectivity; no SSH listener is exposed by the security group.
3. cloud-init installs Docker from Docker's official Ubuntu repository.
4. It waits for the exact Terraform EBS volume ID, matching the Nitro device serial before any formatting.
5. A signature-free device receives ext4 only when the one-time `initialize_blank_data_volume=true` authorization is present. Missing authorization, partitions, inspection errors, signatures, and existing non-ext4 filesystems all fail closed.
6. It mounts by filesystem UUID at `/srv/pz`, clones the configured public HTTPS repository, and verifies detached HEAD exactly matches the configured 40-character commit SHA.
7. `install-host.sh` generates secrets only if absent, installs the watchdog, and enables systemd.
8. `pz-stack.service` grows ext4 if EBS was enlarged, validates Compose, and launches the container without treating temporary RCON unavailability as permission to stop it. Production sets Docker restart policy to `no`, so only this mount-gated systemd unit can start the container during boot.
9. `pz-watchdog.service` starts after the Compose launch. It treats startup RCON uncertainty conservatively; the Windows start helper separately waits for process health plus valid RCON before reporting success.

Bootstrap network/package/Git operations have explicit deadlines. A cloud-init or game-readiness failure leaves the host online for SSM diagnosis because management failure cannot prove zero players. Operators should enable the optional AWS Budget and use the guarded stop helper after diagnosis.

## Graceful Container Stop

The PZ process is attached to a private FIFO used as its local admin console. On `SIGTERM`, the entrypoint sends `save`, waits `SHUTDOWN_SAVE_SECONDS`, sends `quit`, and waits up to `SHUTDOWN_GRACE_SECONDS`. Only a hung process is escalated to `SIGTERM`/`SIGKILL`; console-write failure or escalation makes PID 1 exit nonzero. Host automation captures and reverifies the original container ID and accepts only the exact clean exited state: not running, restarting, dead, or OOM-killed, with exit code `0` and no Docker error. Compose's 120-second stop grace exceeds the default internal sequence.

Current stable Build 42 keeps launcher JVM flags in `ProjectZomboid64.json`. After each SteamCMD update, the entrypoint requires exactly one recognized `-Xmx` and at most one `-Xms`, writes the configured values, and validates the result with `jq`. If upstream changes that contract, startup fails clearly instead of silently using an unsafe allocation.

The current official `start-server.sh` ends with unconditional `exit 0`, which masks a killed Java process. The entrypoint requires that exact upstream contract, generates a colocated wrapper whose final exit preserves the launcher status, and propagates nonzero exits through container PID 1. If upstream changes the script, startup fails for review instead of accepting an unverified transformation.

## Configuration Merge

PZ modifies its INI with world identity fields. Blind template replacement can invalidate characters. The entrypoint therefore renders the versioned managed keys and merges them into the persistent INI. It preserves every unowned key and generated identity value.

Current Build 42 generates vanilla sandbox/spawn Lua files. Future customization should promote reviewed current files into explicit deployment inputs; an incomplete Build 41 template is not shipped.

## Verified PZ Assumptions

Verified on 2026-08-19 against current primary/official sources and a real stable server installation:

- The [official PZwiki dedicated-server guide](https://pzwiki.net/wiki/Dedicated_server), revised for stable 42.20, identifies dedicated-server Steam App ID `380870`, anonymous SteamCMD installation, Linux `start-server.sh`, `$HOME/Zomboid` data, and public `16261/udp` plus `16262/udp`.
- The [official PZwiki server-settings reference](https://pzwiki.net/wiki/Server_settings) identifies `PauseEmpty`, `DefaultPort`, `UDPPort`, `RCONPort`, `RCONPassword`, `Mods`, and `WorkshopItems` behavior.
- The [official PZwiki startup reference](https://pzwiki.net/wiki/Startup_parameters) identifies `-servername`, `-adminusername`, `-adminpassword`, and JVM memory semantics. That page currently carries an older-version warning and still lists `-statistic`; the real stable 42.20.3 server reports that option as unknown, so this repository deliberately omits it and verifies launcher behavior against the installed artifact.
- The [Project Zomboid site](https://projectzomboid.com/blog/news/2026/) reported stable Build `42.20.3` at verification time.
- Valve's [SteamCMD documentation](https://developer.valvesoftware.com/wiki/SteamCMD) is the installation authority; the image retrieves SteamCMD from Valve's CDN over TLS.
- A real anonymous SteamCMD App `380870` installation started successfully, exposed the expected launcher/configuration files, returned the strict `Players connected (0):` RCON response, confirmed `World saved`, and exited cleanly through the save/quit signal path.

PZ and Steam branches evolve. Re-check these sources before changing ports, branches, startup parameters, or generated settings.

## Deliberate Constraints

- No Elastic IP: lower fixed cost, changing address after stop/start.
- No NAT Gateway: the single host is in a public subnet with tightly scoped ingress.
- No SSH: SSM only.
- No CloudWatch Agent by default: on-demand metrics avoid paid custom ingestion.
- Optional AWS Budget notifications are account-wide and advisory; they never trigger lifecycle actions.
- No automatic mod manager, Discord bot, web panel, Kubernetes, ECS, or fake live resize.
- No automatic fixed uptime stop. Player activity can keep the host up indefinitely.
