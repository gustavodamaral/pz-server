# AWS Operations

## Provisioned Resources

- Dedicated IPv4 VPC, one public subnet, Internet Gateway, and route table
- Security group with only verified Build 42 `16261/udp` and `16262/udp` ingress
- EC2 instance with no key pair/public SSH, IMDSv2 required, guest shutdown set to `stop`
- IAM role/profile with `AmazonSSMManagedInstanceCore`
- Disposable encrypted 32 GiB gp3 root volume with headroom for one isolated Steam candidate
- Independent encrypted 40 GiB gp3 world volume with baseline 3,000 IOPS/125 MiB/s and `prevent_destroy`
- Optional account-wide AWS Budget with direct warning/critical email notifications and no actions

There is no EIP, NAT Gateway, Route53 record, backup vault, CloudWatch custom metric/log subscription, or snapshot schedule by default.

## Before Apply

1. Push a reviewed commit to the configured credential-free `repository_url` and put its exact 40-character SHA in the bootstrap-only `repository_ref`.
2. Authenticate AWS CLI with a non-root principal and confirm the target account: `aws sts get-caller-identity`.
3. Review current EC2/EBS/public IPv4 pricing and service quotas. Terraform will reject an explicit AZ, or fail automatic selection, unless both normal and party instance types are offered there.
4. Copy `terraform.tfvars.example`; put only non-secret infrastructure values in it. For an initial blank volume only, explicitly set `initialize_blank_data_volume=true`.
5. Arrange secure remote state before collaboration. Application secrets are absent, but Terraform state still contains infrastructure identifiers and user data.
6. Run `init`, `fmt -check`, `validate`, and inspect a saved plan.
7. Immediately after initial creation, set `initialize_blank_data_volume=false` and apply again. User data is creation-only and ignored on the existing host, so this disarms future hosts without replacing the current instance. Never leave format authorization armed.

No automation in this repository runs `terraform apply`.

## Bootstrap Diagnostics

The Ubuntu AMI normally brings SSM online before application bootstrap completes. Through Session Manager inspect:

```bash
sudo cloud-init status --long
sudo journalctl -u cloud-final -b
sudo journalctl -u pz-stack -b
sudo journalctl -u pz-watchdog -b
sudo docker compose --project-directory /opt/pz-stack --env-file /srv/pz/secrets.env ps
sudo pzctl logs
```

Common first-boot blockers are missing one-time initialization authorization, an unreachable/private Git URL, a commit not pushed, Steam download delay, Docker Hub/GitHub/Valve egress failure, or the EBS attachment not arriving before the guarded timeout. Network/package/Git operations are bounded, but a failed host remains online for SSM diagnosis because player state is unknown. Enable the optional AWS Budget and stop a failed host through the guarded helper after diagnosis.

`pz-stack.service` holds the shared lifecycle lock and waits for Compose process health, including any bounded pre-session Steam candidate transaction. `Start-PZ.ps1` separately requires exact RCON, updater acceptance, and an active watchdog before printing the current address/build. A readiness timeout does not automatically stop the container or host because player state is unknown. Inspect exact Docker/RCON/updater state before any EC2 action.

## Secret Lifecycle

`install-host.sh` generates cryptographically random hexadecimal admin, server, and RCON passwords only if `/srv/pz/secrets.env` does not exist. The file is root-owned `0600` on persistent EBS, so EC2 replacement preserves identity and credentials. Docker receives values at container creation; host root can inspect them, which is inherent for a locally managed container.

Rotate during a maintenance window:

1. Back up and stop PZ.
2. Edit `/srv/pz/secrets.env` through SSM as root.
3. Preserve valid Compose dotenv syntax and single-line values.
4. Start PZ and test RCON/server access.
5. The admin startup argument is used only before the server database exists; rotate an established PZ admin account through supported PZ administration rather than assuming the env value rewrites it.

## Normal and Party Modes

The Windows helper discovers the instance by tags and uses Terraform-managed `PZNormalInstanceType`/`PZPartyInstanceType` tags as its allowlist unless parameters or the `.env` helper allowlist override them. `-Mode Party` can modify type only after stopped state is confirmed. EC2 cannot vertically resize live. Terraform's documented `ignore_changes = [ami, instance_type, user_data]` prevents an unrelated apply from reverting a deliberate runtime choice or applying bootstrap data to an existing host.

The defaults are `r7a.large` (2 vCPUs, 16 GiB) for normal sessions and `m7a.xlarge` (4 vCPUs, 16 GiB) for CPU-heavier party sessions. The production container cap is four CPUs, so it naturally receives the two available in normal mode and can use four in party mode. Verify current AWS specifications and availability before relying on them.

This also means changing `normal_instance_type` in Terraform does not resize an existing instance automatically. Use the stopped helper or deliberately revise the lifecycle strategy in a reviewed change.

Terraform queries `DescribeInstanceTypeOfferings` for both configured types, intersects those results with standard currently available AZs, sorts the common names, and uses the first name when `availability_zone` is null. Enabled Local Zones are excluded to prevent an edge location from silently changing geography or cost. An explicit AZ must be in the same common set. Terraform requires both types to be non-bare-metal and support the selected Ubuntu AMI's `x86_64`, encrypted EBS-root, HVM, and On-Demand contract, but deliberately imposes no universal 16 GiB minimum. The explicit `PZ_XMS`, `PZ_XMX`, and container limit are never adjusted from instance metadata; operators selecting less memory must test and configure those values themselves. These account/region queries occur during `plan`; CI runs only account-independent `validate` and never needs AWS credentials.

After first creation, copy the `availability_zone` output into the explicit variable. This pins the AZ-scoped world volume against future offering-catalog changes; any incompatible type change then fails with a direct message instead of proposing an AZ move. Existing `prevent_destroy` remains the final barrier against accidental volume relocation.

## Public Address

AWS releases the auto-assigned public IPv4 on stop and normally assigns a new one on start. `Start-PZ.ps1` always reads the current address after readiness. An optional Route53/DDNS layer can be added later without changing PZ, but is intentionally absent now.

## Cost Alerts

Cost alerts are disabled by default. To enable them, set:

```hcl
enable_cost_alerts   = true
billing_alert_email  = "operator@example.com"
billing_warning_usd  = 7
billing_critical_usd = 10
```

Terraform creates one recurring monthly `COST` budget for the entire current AWS account. It sends direct email when either actual or forecast cost exceeds the absolute warning or critical threshold. Account-wide scope is intentional: it also catches a forgotten volume, public IPv4, snapshot, or unrelated resource. The email address is not a secret, but it is stored in Terraform state and should be handled as operator information.

This configuration creates no AWS Budget action, SNS topic, Budget Report, IAM role, or automatic EC2 stop. AWS currently states that budget monitoring and ordinary notifications are free; action-enabled budgets and delivered Budget Reports have separate pricing. Direct `EMAIL` subscribers are used, so the AWS-documented SNS subscription-confirmation step does not apply. Verify delivery and spam filtering after creation.

AWS billing data is delayed and currently updates at least daily. Actual alerts are sent once per budget period when first crossed. Forecast alerts can repeat and require approximately five weeks of usage history before AWS can generate forecasts. These notifications are an independent cost backstop, never a real-time lifecycle or player-safety signal.

The Terraform principal needs AWS Budgets read/create/update/delete and notification-subscriber permissions when this option is enabled. Those permissions belong to the provisioning principal, not the EC2 instance role. Review the current [AWS Budgets pricing](https://aws.amazon.com/aws-cost-management/aws-budgets/pricing/) and [alert behavior](https://docs.aws.amazon.com/cost-management/latest/userguide/budgets-best-practices.html) before changing the design.

## Application Deployments

Terraform user data is creation-time bootstrap input. `repository_ref`, `repository_url`, the bootstrap script, and rendered cloud-init are ignored for updates to an existing EC2 instance, and `user_data_replace_on_change` is disabled. Editing application/config references followed by an unrelated apply therefore cannot replace or stop the host. A future intentional EC2 creation still uses the current exact SHA and bootstrap inputs.

Deploy routine repository changes through the constrained SSM helper instead:

```powershell
$commit = git rev-parse HEAD
.\scripts\windows\Deploy-PZ.ps1 -CommitSha $commit
```

The host transaction performs these steps:

1. Require an exact 40-character SHA, a clean production checkout, and a credential-free HTTPS origin.
2. Fetch and verify that SHA before any service interruption, then stage it temporarily for Bash syntax and production Compose rendering checks.
3. Take the same lifecycle lock used by backups, query real players through RCON, confirm save, and require an exact clean container stop.
4. Disable any legacy Docker-managed restart policy, record a durable root-only pending transaction, preserve both the previous deployment orchestrator and watchdog environment, check out detached HEAD, atomically reinstall repository-owned host tooling/services, and require Docker health, valid RCON, accepted game-update state, and a continuously active watchdog service. The pending directory is mounted read-only into the game container, suppressing Steam checks and flat-layout migration throughout target start and rollback.
5. Record the deployed SHA only after readiness. An interrupted exact-SHA retry is routed to the preserved orchestrator and uses the already-fetched local commit instead of treating Git `HEAD` as completion or depending on the network. While pending, systemd start conditions and an independent transient guardian require the live lock-owning transaction to remain present; interruption stops partial services.
6. On failure, rollback uses the preserved previous watchdog and venv without a new package download. It proceeds only after the target container is missing, cleanly stopped, or passes the guarded stop. Connected players, `UNKNOWN`, OOM, nonzero exit, restart/dead state, Docker error, or inspection failure prohibit automatic rollback and leave the host online for diagnosis.

`-AllowConnectedPlayers` is an explicit announced-maintenance override for known players; it never overrides `UNKNOWN`. The helper accepts no branch, repository URL, credential, or arbitrary command text. Runtime secrets and operational values in `/srv/pz/secrets.env` remain persistent host configuration and are not moved into Terraform.

New hosts record their bootstrap SHA after host installation. For a host created before this deployment marker existed, first call `Deploy-PZ.ps1` with its current `git -C /opt/pz-stack rev-parse HEAD`; this intentionally reinstalls the same reviewed commit to establish a baseline before any different SHA is accepted. The SSM command execution timeout defaults to 150 minutes so the bounded target plus rollback path fits; command delivery has a separate five-minute deadline and the local waiter covers both periods plus polling margin. A timeout leaves the pending marker and requires retrying the same SHA.

Changing installer defaults generally does not rewrite an existing `secrets.env`; adjust a legacy `PZ_XMX=12g` to `8g` during a measured maintenance window. The installer deliberately makes `RESTART_POLICY=no` a managed production invariant. It also migrates legacy `UPDATE_ON_START=false` to `PZ_UPDATE_POLICY=manual` (and `true` to `stable-on-start`) rather than silently changing behavior; new hosts default to automatic public Stable checks. Production systemd, not Docker's independent restart manager, owns boot ordering and the shared lifecycle lock so the container cannot start before `/srv/pz` is mounted or race a backup/deployment.

## Game Updates

Steam game releases are independent from exact-SHA repository deployments. New hosts use `PZ_UPDATE_POLICY=stable-on-start`; the check runs only while starting a new gameplay session. No AWS timer, poller, or scheduled wakeup is created. Empty `STEAM_BRANCH` is public Stable, non-public branches require `manual`, automatic downgrade is prohibited, and configured mods block automatic updating unless explicitly allowed.

For a reviewed maintenance update or repair:

```bash
sudo pzctl update
sudo pzctl update --validate  # repair only
```

`pzctl update` takes `/run/pz-server-backup.lock`, requires known zero players and a confirmed save/clean stop when running, executes the same image-contained updater against the production mounts, then restarts and requires process health, exact RCON, candidate acceptance, and watchdog activity. Candidate installation is isolated under `/var/lib/pz-server/releases`; the world backup is written to `/srv/pz/backups/pre-update` before activation. Default retention is three and may not be set below two.

Use `sudo pzctl status --json` or `Get-PZStatus.ps1` to inspect `update.state`, current/candidate/blocked build, last result, and observed PZ version/revision. `failed-before-world-open` leaves the known-good release online. `failed-after-world-open` never causes automatic binary rollback; review the current release and matching pre-update world archive before any recovery. A managed `active-release` pointer also makes deployment preflight reject repository commits that predate this layout.

## Intentional EC2 Replacement

Terraform always creates and configures EC2 with API termination protection enabled. `allow_instance_replacement=true` persists one-time provider authorization in the current instance's Terraform state; it does not expose the instance to direct API termination. The AWS provider can remove protection during deletion only when `force_destroy=true` was already recorded in prior state, so arming and replacing in one apply is deliberately unsupported.

Use this staged procedure:

1. Create/verify a current off-volume backup, confirm no players, and run `.\scripts\windows\Stop-PZ.ps1` so EC2 reaches `stopped`.
2. Plan with `-var='allow_instance_replacement=true'` and no replacement request. Inspect that the only intended EC2 change arms `force_destroy` while API termination protection remains enabled, then apply that saved plan.
3. Create a second saved plan with `allow_instance_replacement=false` and `-replace=aws_instance.server`. The old state supplies the one-time deletion authorization while the replacement is immediately protected and disarmed. Verify that the protected `aws_ebs_volume.world` is unchanged and only the disposable instance/root volume and attachment lifecycle are affected.
4. Apply the replacement plan, wait for bootstrap/readiness, and complete the acceptance checks before inviting players.
5. Verify in state and EC2 that the replacement has `force_destroy=false` and API termination protection enabled.

Never combine steps 2 and 3. Until a false-only disarm or the reviewed replacement plan is applied, prior state remains armed even if a later command merely supplies the variable as false. If replacement is abandoned, apply a saved plan whose only change is `force_destroy: true -> false`; an accidental replacement in that disarm plan could consume the prior authorization. This gate never disables EC2 API termination protection or `prevent_destroy` on the world volume.

## Persistent Volume and Replacement

The volume is AZ-scoped. Keep the instance/subnet in that AZ. cloud-init identifies it by exact volume ID/serial and distinguishes recognized ext4 from absence of a recognized filesystem. It can format only when the explicit one-time Terraform permission is true and the whole device has no child partitions or signatures. Inspection errors, non-ext4 filesystems, and missing authorization fail without modifying the device. The disposable root volume must still have enough free space for one active release plus one isolated candidate; the updater checks measured free space and fails before world access rather than deleting data.

On intentional EC2 replacement, follow the staged protection workflow above. Terraform detaches/reattaches through `aws_volume_attachment`; never force-detach a mounted active world volume.

`prevent_destroy` blocks ordinary destroy and replacements requiring volume destruction. This is deliberate. The retain/decommission sequence is in the README. To bring a retained volume back under management, add/import it only after matching AZ, encryption, and attachment design.

To grow the volume, increase `data_volume_size_gib`; EBS and ext4 reductions are unsupported. After applying the increase, gracefully restart `pz-stack.service` while no players are connected. Its `pz-resize-data-volume` pre-start hook runs idempotent online `resize2fs`. Confirm with `findmnt /srv/pz` and `df -h /srv/pz`.

## Snapshot Backup and Restore

A conservative manual snapshot workflow:

1. Confirm no players: `sudo pzctl players`.
2. Run `sudo pzctl graceful-stop`.
3. Run `sudo sync && sudo umount /srv/pz`; do not continue unless `mountpoint /srv/pz` confirms it is unmounted. This flushes filesystem state before the snapshot.
4. Create an EBS snapshot of the output `persistent_volume_id` in the AWS console/CLI.
5. Run `sudo mount /srv/pz`, verify `mountpoint /srv/pz`, then run `sudo pzctl start`.
6. Tag the snapshot with project, environment, UTC date, and PZ build, and apply an intentional retention policy; snapshots incur storage cost.

Rollback to a snapshot without overwriting the protected current volume:

1. Stop PZ/EC2 gracefully and record both the current volume ID and rollback snapshot ID.
2. Perform only the preparatory `allow_instance_replacement=true` apply from [Intentional EC2 Replacement](#intentional-ec2-replacement). Do this before changing volume state/configuration so the plan can contain only the maintenance gate.
3. Run `terraform -chdir=terraform destroy -target=aws_volume_attachment.world -var='allow_instance_replacement=true'` and inspect that only the attachment is removed.
4. Run `terraform -chdir=terraform state rm aws_ebs_volume.world`. This deliberately retains the old volume outside Terraform; record it because it continues to cost money.
5. Set `data_volume_snapshot_id` to the rollback snapshot, keep `initialize_blank_data_volume=false`, and ensure `data_volume_size_gib` is at least the snapshot source size.
6. Save a plan with `allow_instance_replacement=false` and `-replace=aws_instance.server`. It must create the new protected volume, replace the instance, disarm the replacement, and leave the retained old volume untouched. The explicit replacement is required because a volume removed from state is a pure create, not an update that Terraform can use as a replacement trigger.
7. Apply, verify termination protection, validate world/player identity through SSM and a test client, and keep the old volume until acceptance. Delete or archive it later only through a separate explicit decision.

For a brand-new environment with no current volume, setting `data_volume_snapshot_id` before first creation is sufficient and initialization permission remains false.

`pz-backup` creates a checksum sidecar, retains seven completed manual `pz-world-*` archives by default, and uses the exclusive lifecycle lock. Automatic updates separately retain at least two verified `pz-pre-update-*` archives plus checksum/metadata sidecars under a container-writable subdirectory. Both classes remain on the same EBS volume. Copy recovery artifacts to separately controlled encrypted storage and treat them as secrets because PZ configuration contains server/RCON credentials.

Restore a Linux tar backup after copying the archive and sidecar back to `/srv/pz/backups`:

```bash
sudo pzctl graceful-stop
cd /srv/pz/backups && sha256sum --check pz-world-YYYYMMDDTHHMMSSZ.tar.gz.sha256
sudo mv /srv/pz/data /srv/pz/data.before-restore
sudo tar --extract --gzip --file /srv/pz/backups/pz-world-YYYYMMDDTHHMMSSZ.tar.gz --directory /srv/pz
sudo chown -R 1000:1000 /srv/pz/data
sudo pzctl start
```

Keep `data.before-restore` until world, player identity, and administrator access are verified. The tar archive does not include root-owned `/srv/pz/secrets.env`; preserve that file separately through the protected volume/snapshot lifecycle.

For optional automated EBS snapshots, use EC2 Console **Lifecycle Manager > EBS snapshot policy**, target the volume tag `DataClass=persistent-world`, use AWS's default DLM role, choose a low-frequency schedule, and retain a small explicit count. Tag snapshots and monitor storage charges. An uncoordinated DLM snapshot is crash-consistent; retain periodic `pz-backup` archives or use a separately reviewed SSM save/stop/snapshot/start orchestration when application consistency is required. This repository does not provision the policy by default so an infrastructure apply cannot silently create indefinite paid retention.

## IAM and Network Notes

AWS documents `AmazonSSMManagedInstanceCore` as the standard core instance policy. The host receives no EC2 stop permission because it uses guest poweroff; this avoids broader AWS API credentials on the machine. SSM and package/content traffic use outbound internet through the public IPv4.

Gameplay defaults to `0.0.0.0/0` because friends may have changing residential IPs. Restrict `allowed_game_cidrs` where practical. A server password remains necessary either way. No SG ingress exists for SSH or RCON.

## CloudWatch Compatibility

Basic EC2 metrics work without an agent. `enable_detailed_monitoring=true` increases frequency and can add cost. The status CLI emits stable JSON, including a whitelisted nested update object, suitable for a future least-privilege CloudWatch Agent configuration. If enabled later, attach only the required log/metric permissions and set explicit retention; do not attach broad CloudWatch policy preemptively.
