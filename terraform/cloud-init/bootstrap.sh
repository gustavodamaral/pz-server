#!/usr/bin/env bash
set -Eeuo pipefail

readonly BOOTSTRAP_ENV=/etc/pz-bootstrap.env
readonly DATA_MOUNT=/srv/pz
readonly PROJECT_DIRECTORY=/opt/pz-stack

log() {
    local level="$1"
    shift
    printf '%s %-7s %s\n' "$(date --utc +'%Y-%m-%dT%H:%M:%SZ')" "${level}" "$*"
}

die() {
    log ERROR "$*"
    exit 1
}

[[ $(id -u) -eq 0 ]] || die "First-boot bootstrap must run as root."
[[ -r "${BOOTSTRAP_ENV}" ]] || die "Missing ${BOOTSTRAP_ENV}."
# The file is root-owned cloud-init input containing no secrets.
# shellcheck disable=SC1090
source "${BOOTSTRAP_ENV}"

DATA_VOLUME_ID="$(printf '%s' "${DATA_VOLUME_ID_BASE64}" | base64 --decode)"
REPOSITORY_URL="$(printf '%s' "${REPOSITORY_URL_BASE64}" | base64 --decode)"
REPOSITORY_REF="$(printf '%s' "${REPOSITORY_REF_BASE64}" | base64 --decode)"

install_host_packages() {
    log INFO "Installing Docker Engine and host bootstrap dependencies."
    export DEBIAN_FRONTEND=noninteractive
    timeout 20m apt-get update
    timeout 20m apt-get install --no-install-recommends --yes \
        ca-certificates curl e2fsprogs git gnupg jq nvme-cli openssl python3-venv util-linux

    install -m 0755 -d /etc/apt/keyrings
    curl --connect-timeout 10 --max-time 300 --fail --location --show-error --silent \
        https://download.docker.com/linux/ubuntu/gpg \
        --output /etc/apt/keyrings/docker.asc
    chmod 0644 /etc/apt/keyrings/docker.asc
    # shellcheck disable=SC1091
    source /etc/os-release
    printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu %s stable\n' \
        "$(dpkg --print-architecture)" "${VERSION_CODENAME}" \
        > /etc/apt/sources.list.d/docker.list
    timeout 20m apt-get update
    timeout 20m apt-get install --no-install-recommends --yes \
        containerd.io docker-buildx-plugin docker-ce docker-ce-cli docker-compose-plugin
    systemctl enable --now docker

    systemctl enable --now amazon-ssm-agent 2>/dev/null \
        || systemctl enable --now snap.amazon-ssm-agent.amazon-ssm-agent.service 2>/dev/null \
        || log WARNING "SSM Agent unit was not found; the Ubuntu AMI normally provides it."
}

find_data_device() {
    local expected_serial="${DATA_VOLUME_ID//-/}"
    local device serial
    for _ in $(seq 1 120); do
        for device in /dev/nvme*n1 /dev/xvdf /dev/sdf; do
            [[ -b "${device}" ]] || continue
            serial="$(lsblk --nodeps --noheadings --output SERIAL "${device}" 2>/dev/null | tr --delete ' -')"
            if [[ "${serial}" == "${expected_serial}" ]]; then
                printf '%s\n' "${device}"
                return 0
            fi
        done
        sleep 5
    done
    return 1
}

mount_data_volume() {
    local device filesystem_status filesystem signatures signature_status uuid
    log INFO "Waiting for persistent EBS volume ${DATA_VOLUME_ID}."
    device="$(find_data_device)" || die "Persistent EBS volume ${DATA_VOLUME_ID} was not attached within 10 minutes."
    set +e
    filesystem="$(blkid --probe --output value --match-tag TYPE "${device}" 2>/dev/null)"
    filesystem_status=$?
    set -e
    if (( filesystem_status != 0 && filesystem_status != 2 )); then
        die "Could not safely inspect the persistent volume filesystem (blkid status ${filesystem_status})."
    fi
    if [[ -z "${filesystem}" ]]; then
        [[ "${INITIALIZE_DATA_VOLUME}" == true ]] \
            || die "No filesystem was detected. Refusing to format without initialize_blank_data_volume=true."
        [[ "$(lsblk --noheadings --raw --output TYPE "${device}" | wc --lines)" -eq 1 ]] \
            || die "Persistent volume has child partitions; refusing to format it."
        set +e
        signatures="$(wipefs --no-act --noheadings --output TYPE "${device}" 2>/dev/null)"
        signature_status=$?
        set -e
        (( signature_status == 0 )) \
            || die "Could not safely inspect persistent volume signatures (wipefs status ${signature_status})."
        [[ -z "${signatures//[[:space:]]/}" ]] \
            || die "Persistent volume contains an unrecognized signature; refusing to format it."
        log WARNING "One-time initialization is authorized; creating ext4 on signature-free volume ${DATA_VOLUME_ID}."
        mkfs.ext4 -m 0 -L pz-data "${device}"
        filesystem=ext4
    fi
    [[ "${filesystem}" == ext4 ]] || die "Persistent volume uses unsupported filesystem '${filesystem}'; refusing to modify it."
    uuid="$(blkid --output value --match-tag UUID "${device}")"
    [[ -n "${uuid}" ]] || die "Could not read persistent volume UUID."

    install -d -m 0755 "${DATA_MOUNT}"
    if ! grep --quiet --fixed-strings "UUID=${uuid} ${DATA_MOUNT} " /etc/fstab; then
        printf 'UUID=%s %s ext4 defaults,nofail,x-systemd.device-timeout=10min 0 2\n' \
            "${uuid}" "${DATA_MOUNT}" >> /etc/fstab
    fi
    if ! mountpoint --quiet "${DATA_MOUNT}"; then
        mount "${DATA_MOUNT}"
    fi
    mountpoint --quiet "${DATA_MOUNT}" || die "Persistent data mount verification failed."
    if [[ "${INITIALIZE_DATA_VOLUME}" == true ]]; then
        sed --in-place \
            's/^INITIALIZE_DATA_VOLUME=true$/INITIALIZE_DATA_VOLUME=false/' \
            "${BOOTSTRAP_ENV}"
        grep --quiet --fixed-strings 'INITIALIZE_DATA_VOLUME=false' "${BOOTSTRAP_ENV}" \
            || die "Could not consume the one-time data-volume initialization authorization."
        log INFO "Consumed the host-local data-volume initialization authorization."
    fi
    install -d -m 0750 "${DATA_MOUNT}/data" "${DATA_MOUNT}/backups"
    chown -R 1000:1000 "${DATA_MOUNT}/data"
}

deploy_repository() {
    [[ "${REPOSITORY_URL}" == https://* ]] || die "Repository URL must use HTTPS."
    if [[ ! -d "${PROJECT_DIRECTORY}/.git" ]]; then
        rm -rf "${PROJECT_DIRECTORY}"
        timeout 5m git clone --filter=blob:none --no-checkout "${REPOSITORY_URL}" "${PROJECT_DIRECTORY}"
    fi
    timeout 5m git -C "${PROJECT_DIRECTORY}" fetch --depth=1 origin "${REPOSITORY_REF}"
    git -C "${PROJECT_DIRECTORY}" checkout --detach --force FETCH_HEAD
    [[ "$(git -C "${PROJECT_DIRECTORY}" rev-parse HEAD)" == "${REPOSITORY_REF,,}" ]] \
        || die "Checked-out repository commit does not match the requested immutable SHA."
    PZ_DATA_ROOT="${DATA_MOUNT}" bash "${PROJECT_DIRECTORY}/scripts/linux/install-host.sh"
    install -d -m 0700 /var/lib/pz-deploy
    printf '%s\n' "${REPOSITORY_REF,,}" > /var/lib/pz-deploy/deployed-commit.tmp
    chmod 0600 /var/lib/pz-deploy/deployed-commit.tmp
    mv /var/lib/pz-deploy/deployed-commit.tmp /var/lib/pz-deploy/deployed-commit
}

main() {
    install_host_packages
    mount_data_volume
    deploy_repository
    log INFO "Host bootstrap complete. Project Zomboid startup continues under systemd."
}

main "$@"
