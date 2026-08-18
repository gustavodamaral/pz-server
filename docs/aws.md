# AWS Operations

## Provisioned Resources

- Dedicated IPv4 VPC, one public subnet, Internet Gateway, and route table
- Security group with only verified Build 42 `16261/udp` and `16262/udp` ingress
- EC2 instance with no key pair/public SSH, IMDSv2 required, guest shutdown set to `stop`
- IAM role/profile with `AmazonSSMManagedInstanceCore`
- Disposable encrypted gp3 root volume
- Independent encrypted 40 GiB gp3 world volume with baseline 3,000 IOPS/125 MiB/s and `prevent_destroy`

There is no EIP, NAT Gateway, Route53 record, backup vault, CloudWatch custom metric/log subscription, or snapshot schedule by default.

## Before Apply

1. Push a reviewed commit to the configured credential-free `repository_url` and put its exact 40-character SHA in `repository_ref`.
2. Authenticate AWS CLI with a non-root principal and confirm the target account: `aws sts get-caller-identity`.
3. Review current EC2/EBS/public IPv4 pricing and service quotas in the chosen region/AZ.
4. Copy `terraform.tfvars.example`; put only non-secret infrastructure values in it. For an initial blank volume only, explicitly set `initialize_blank_data_volume=true`.
5. Arrange secure remote state before collaboration. Application secrets are absent, but Terraform state still contains infrastructure identifiers and user data.
6. Run `init`, `fmt -check`, `validate`, and inspect a saved plan.
7. After the initial server becomes healthy, stop it gracefully, set `initialize_blank_data_volume=false`, and apply the reviewed instance-replacement plan. Never leave format authorization armed.

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

Common first-boot blockers are missing one-time initialization authorization, an unreachable/private Git URL, a commit not pushed, Steam download delay, Docker Hub/GitHub/Valve egress failure, or the EBS attachment not arriving before the guarded timeout. Network/package/Git operations are bounded, but a failed host remains online for SSM diagnosis because player state is unknown. Configure an AWS Billing alarm and stop it through the guarded helper after diagnosis.

`pz-stack.service` considers a successful Compose launch complete; `Start-PZ.ps1` separately waits for process health and RCON. A readiness timeout does not automatically stop the container or host because player state is unknown. Inspect exact Docker/RCON state before any EC2 action.

## Secret Lifecycle

`install-host.sh` generates cryptographically random hexadecimal admin, server, and RCON passwords only if `/srv/pz/secrets.env` does not exist. The file is root-owned `0600` on persistent EBS, so EC2 replacement preserves identity and credentials. Docker receives values at container creation; host root can inspect them, which is inherent for a locally managed container.

Rotate during a maintenance window:

1. Back up and stop PZ.
2. Edit `/srv/pz/secrets.env` through SSM as root.
3. Preserve valid Compose dotenv syntax and single-line values.
4. Start PZ and test RCON/server access.
5. The admin startup argument is used only before the server database exists; rotate an established PZ admin account through supported PZ administration rather than assuming the env value rewrites it.

## Normal and Party Modes

The Windows helper discovers the instance by tags and uses Terraform-managed `PZNormalInstanceType`/`PZPartyInstanceType` tags as its allowlist unless parameters or the `.env` helper allowlist override them. `-Mode Party` can modify type only after stopped state is confirmed. EC2 cannot vertically resize live. Terraform's documented `ignore_changes = [ami, instance_type]` prevents an unrelated apply from reverting a deliberate runtime choice.

The defaults are `r7a.large` (2 vCPUs, 16 GiB) for normal sessions and `m7a.xlarge` (4 vCPUs, 16 GiB) for CPU-heavier party sessions. The production container cap is four CPUs, so it naturally receives the two available in normal mode and can use four in party mode. Verify current AWS specifications and availability before relying on them.

This also means changing `normal_instance_type` in Terraform does not resize an existing instance automatically. Use the stopped helper or deliberately revise the lifecycle strategy in a reviewed change.

## Public Address

AWS releases the auto-assigned public IPv4 on stop and normally assigns a new one on start. `Start-PZ.ps1` always reads the current address after readiness. An optional Route53/DDNS layer can be added later without changing PZ, but is intentionally absent now.

## Persistent Volume and Replacement

The volume is AZ-scoped. Keep the instance/subnet in that AZ. cloud-init identifies it by exact volume ID/serial and distinguishes recognized ext4 from absence of a recognized filesystem. It can format only when the explicit one-time Terraform permission is true and the whole device has no child partitions or signatures. Inspection errors, non-ext4 filesystems, and missing authorization fail without modifying the device.

On intentional EC2 replacement, stop PZ first and verify a recent backup. Terraform detaches/reattaches through `aws_volume_attachment`; never force-detach a mounted active world volume.

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
2. Run `terraform -chdir=terraform destroy -target=aws_volume_attachment.world` and inspect that only the attachment is removed.
3. Run `terraform -chdir=terraform state rm aws_ebs_volume.world`. This deliberately retains the old volume outside Terraform; record it because it continues to cost money.
4. Set `data_volume_snapshot_id` to the rollback snapshot, keep `initialize_blank_data_volume=false`, and ensure `data_volume_size_gib` is at least the snapshot source size.
5. Save and inspect a normal Terraform plan. It should create a new protected volume from the snapshot, replace the disposable EC2 instance because its exact volume ID is in first-boot data, and attach the new volume. It must not delete the retained old volume.
6. Apply, validate world/player identity through SSM and a test client, and keep the old volume until acceptance. Delete or archive it later only through a separate explicit decision.

For a brand-new environment with no current volume, setting `data_volume_snapshot_id` before first creation is sufficient and initialization permission remains false.

`pz-backup` creates a checksum sidecar, retains seven completed archives by default, and uses an exclusive lifecycle lock, but stores archives on the same EBS volume. Copy both files to separately controlled encrypted storage and treat them as secrets because PZ configuration contains server/RCON credentials.

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

Basic EC2 metrics work without an agent. `enable_detailed_monitoring=true` increases frequency and can add cost. The status CLI emits stable JSON suitable for a future least-privilege CloudWatch Agent configuration. If enabled later, attach only the required log/metric permissions and set explicit retention; do not attach broad CloudWatch policy preemptively.
