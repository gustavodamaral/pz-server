# Architecture

## Boundaries

Terraform owns durable infrastructure, not PZ installation details. Its cloud-init payload installs Docker, verifies and mounts the independently created EBS volume, checks out an exact Git commit, and calls `scripts/linux/install-host.sh`. Existing instance user data is deliberately ignored after creation; exact-SHA routine deployments use the guarded SSM/`pzctl deploy` path instead. Application behavior remains reviewable and testable outside HCL.

Docker owns the PZ runtime. The custom Debian image contains Valve SteamCMD, required 32-bit compatibility libraries, checksum-pinned `rcon-cli`, the Python updater, and lifecycle scripts. It does not contain PZ binaries or secrets. Managed PZ releases and update state are under `/opt/pzserver`; all user data is under `/home/pz/Zomboid`.

Host automation owns privileged lifecycle effects. The Python watchdog runs under systemd on AWS, accesses RCON through host loopback, inspects Docker health, and controls Compose. Neither application container receives the Docker socket.

## Persistent and Disposable State

The root EBS volume is encrypted and disposable. It holds Ubuntu, Docker, the Git checkout, the Python virtual environment, updater state, and reacquirable managed Steam releases. The independent encrypted gp3 volume holds `/srv/pz/data`, manual and automatic pre-update backups under `/srv/pz/backups`, and `/srv/pz/secrets.env`.

This split makes EC2 replacement recoverable: attach the same volume in its fixed availability zone, clone the application, regenerate host tooling, and start against the existing world and identity database.

## Boot Sequence

1. EC2 boots the latest selected Canonical Ubuntu 24.04 AMI with IMDSv2 required.
2. SSM Agent establishes outbound management connectivity; no SSH listener is exposed by the security group.
3. cloud-init installs Docker from Docker's official Ubuntu repository.
4. It waits for the exact Terraform EBS volume ID, matching the Nitro device serial before any formatting.
5. A signature-free device receives ext4 only when the one-time `initialize_blank_data_volume=true` authorization is present. Missing authorization, partitions, inspection errors, signatures, and existing non-ext4 filesystems all fail closed.
6. It mounts by filesystem UUID at `/srv/pz`, clones the configured public HTTPS repository, and verifies detached HEAD exactly matches the configured 40-character commit SHA.
7. `install-host.sh` generates secrets only if absent, installs the watchdog, and enables systemd.
8. `pz-stack.service` grows ext4 if EBS was enlarged, validates Compose, then holds the shared lifecycle lock while Compose builds and waits for process health. Production sets Docker restart policy to `no`, so only this mount-gated systemd unit can start the container during boot. A live, PID/start-time-verified `pzctl` authorization prevents lock recursion during guarded lifecycle/deployment operations.
9. Before Java starts, the container checks the configured policy, stages any public Stable update in isolation, verifies a pre-update backup, and atomically selects a release. Docker remains in health `starting` for the bounded update window.
10. `pz-watchdog.service` starts only after the stack reaches process health. `Start-PZ.ps1` separately requires exact RCON, updater acceptance, and a continuously active watchdog before reporting success.

Bootstrap network/package/Git operations have explicit deadlines. A cloud-init or game-readiness failure leaves the host online for SSM diagnosis because management failure cannot prove zero players. Operators should enable the optional AWS Budget and use the guarded stop helper after diagnosis.

## Graceful Container Stop

The PZ process is attached to a private FIFO used as its local admin console. On `SIGTERM`, the entrypoint sends `save`, waits `SHUTDOWN_SAVE_SECONDS`, sends `quit`, and waits up to `SHUTDOWN_GRACE_SECONDS`. Only a hung process is escalated to `SIGTERM`/`SIGKILL`; console-write failure or escalation makes PID 1 exit nonzero. Host automation captures and reverifies the original container ID and accepts only the exact clean exited state: not running, restarting, dead, or OOM-killed, with exit code `0` and no Docker error. Compose's 120-second stop grace exceeds the default internal sequence.

Current stable Build 42 keeps launcher JVM flags in `ProjectZomboid64.json`. For each isolated candidate, the updater parses JSON, requires the dedicated `zombie/network/GameServer` main class, exactly one recognized `-Xmx`, and at most one `-Xms`, then writes explicit configured values. If upstream changes that contract, the candidate is rejected before activation instead of silently using an unsafe allocation.

The current official `start-server.sh` ends with unconditional `exit 0`, which masks a killed Java process. The updater structurally requires one Java probe, one `ProjectZomboid64 "$@"` invocation, one unsupported-64-bit branch, and one terminal `exit 0`. It generates a colocated wrapper that captures the game status immediately and propagates it through container PID 1. Ambiguous or reordered lifecycle commands reject the candidate for review; no whole-file checksum couples updates to harmless upstream comments.

## Game Update Transaction

Repository deployment and Steam game updating are separate release streams. `/var/lib/pz-deploy/deployment-pending` is root-owned and mounted read-only into the game container. Its presence suppresses Steam checks before a target or rollback starts.

`PZ_UPDATE_POLICY=stable-on-start` checks Steam's `public` branch only before a gameplay session. `manual` performs no metadata query for an existing release. Explicit non-public branches require `manual`; configured mods block automatic updates unless separately authorized. There is no timer, external poller, automatic downgrade, or update of a running Java process.

The transaction is:

1. Acquire the host lifecycle lock for systemd/`pzctl` orchestration and the release-volume advisory lock for updater state.
2. Parse Steam App `380870` metadata and compare structured positive build IDs.
3. Download to `releases/.candidate-*`, verify the complete manifest, required executable/artifact set, launcher JSON, generated Bash wrapper, and expected build ID.
4. Archive `/home/pz/Zomboid` to the persistent pre-update directory, write SHA-256/metadata sidecars, verify the archive and checksum, and retain at least two completed recovery points. Any backup failure removes the candidate and preserves the selected release.
5. Promote the candidate and atomically replace text pointers only after durable transaction state records old/new builds and the verified backup.
6. Immediately before candidate launch, persist `world_opened=true`. Exact local RCON plus process health records runtime readiness; AWS additionally requires host/watchdog acceptance.

Failures before world access retain or reselect the known-good release. Only a deterministic launcher/candidate incompatibility blocks that exact build from repeated automatic attempts; transient Steam, disk, backup, and interruption failures retry on a future start. After `world_opened=true`, failure is `failed-after-world-open`; the candidate is stopped but remains selected and automation never starts older binaries against possibly migrated world data. Recovery then requires release notes, logs, a maintenance stop, and an explicit decision to continue forward or restore a compatible world backup.

## Configuration Merge

PZ modifies its INI with world identity fields. Blind template replacement can invalidate characters. The entrypoint therefore renders the versioned managed keys and merges them into the persistent INI. It preserves every unowned key and generated identity value.

Current Build 42 generates vanilla sandbox/spawn Lua files. Future customization should promote reviewed current files into explicit deployment inputs; an incomplete Build 41 template is not shipped.

## Verified PZ Assumptions

Verified on 2026-08-20 against current primary/official sources, direct Steam metadata, and a real stable server installation:

- The [official PZwiki dedicated-server guide](https://pzwiki.net/wiki/Dedicated_server), revised for stable 42.20, identifies dedicated-server Steam App ID `380870`, anonymous SteamCMD installation, Linux `start-server.sh`, `$HOME/Zomboid` data, and public `16261/udp` plus `16262/udp`.
- The [official PZwiki server-settings reference](https://pzwiki.net/wiki/Server_settings) identifies `PauseEmpty`, `DefaultPort`, `UDPPort`, `RCONPort`, `RCONPassword`, `Mods`, and `WorkshopItems` behavior.
- The [official PZwiki startup reference](https://pzwiki.net/wiki/Startup_parameters) identifies `-servername`, `-adminusername`, `-adminpassword`, and JVM memory semantics. That page currently carries an older-version warning and still lists `-statistic`; the real stable 42.20.3 server reports that option as unknown, so this repository deliberately omits it and verifies launcher behavior against the installed artifact.
- The [Project Zomboid site](https://projectzomboid.com/blog/news/2026/) reported Stable `42.20.3` at verification time.
- A direct anonymous SteamCMD `app_info_print 380870` query reported public branch build ID `24775771` with description `42.20.3`; the retained complete manifest (`StateFlags=4`, `UpdateResult=0`) reports the same build. Valve's [SteamCMD documentation](https://developer.valvesoftware.com/wiki/SteamCMD) remains the installation authority, although its anti-bot interstitial prevented automated retrieval during this verification; the image retrieves SteamCMD from Valve's CDN over TLS.
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
