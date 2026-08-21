#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIRECTORY="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
readonly PROJECT_DIRECTORY
readonly PZ_DATA_ROOT="${PZ_DATA_ROOT:-/srv/pz}"
readonly ENV_FILE="${PZ_DATA_ROOT}/secrets.env"
readonly VENV_DIRECTORY=/opt/pz-watchdog-venv
readonly RCON_CLI_VERSION=1.7.7
readonly PRESERVE_WATCHDOG_VENV="${PZ_PRESERVE_WATCHDOG_VENV:-false}"

log() {
    local level="$1"
    shift
    printf '%s %-7s %s\n' "$(date --utc +'%Y-%m-%dT%H:%M:%SZ')" "${level}" "$*"
}

die() {
    log ERROR "$*"
    exit 1
}

install_file_atomically() {
    local source="$1"
    local destination="$2"
    local mode="$3"
    local temporary
    temporary="$(mktemp "${destination}.XXXXXX")"
    if ! install -o root -g root -m "${mode}" "${source}" "${temporary}" \
        || ! mv -- "${temporary}" "${destination}" \
        || ! sync -f "${destination}" \
        || ! sync -f "$(dirname "${destination}")"; then
        rm -f -- "${temporary}"
        die "Could not publish ${destination} atomically."
    fi
}

set_managed_environment_value() {
    local key="$1"
    local value="$2"
    local count temporary
    count="$(grep --count -- "^${key}=" "${ENV_FILE}" || true)"
    if [[ ! "${count}" =~ ^[0-9]+$ ]] || (( count > 1 )); then
        die "${ENV_FILE} must contain at most one ${key} entry."
    fi
    temporary="$(mktemp "${ENV_FILE}.XXXXXX")"
    if ! awk -v key="${key}" -v value="${value}" '
        index($0, key "=") == 1 { print key "=" value; found = 1; next }
        { print }
        END { if (!found) print key "=" value }
    ' "${ENV_FILE}" > "${temporary}" \
        || ! chmod 0600 "${temporary}" \
        || ! chown root:root "${temporary}" \
        || ! mv -- "${temporary}" "${ENV_FILE}" \
        || ! sync -f "${ENV_FILE}" \
        || ! sync -f "$(dirname "${ENV_FILE}")"; then
        rm -f -- "${temporary}"
        die "Could not enforce ${key}=${value} in ${ENV_FILE}."
    fi
}

install_rcon_cli() {
    local architecture archive_arch checksum archive
    if command -v rcon-cli >/dev/null 2>&1; then
        return
    fi
    architecture="$(uname -m)"
    case "${architecture}" in
        x86_64) archive_arch=amd64; checksum=a6faf3d8b8259e88fd0a662dd6baff74a4226bafd96a9f578fcc3f4f534cadf2 ;;
        aarch64) archive_arch=arm64; checksum=05648eb1b2f6bd7b331776baee9e791fb3a938b343fa35e89b663b5527eabe27 ;;
        *) die "Unsupported rcon-cli architecture: ${architecture}" ;;
    esac
    archive="$(mktemp)"
    curl --connect-timeout 10 --max-time 300 --fail --location --show-error --silent \
        --output "${archive}" \
        "https://github.com/itzg/rcon-cli/releases/download/${RCON_CLI_VERSION}/rcon-cli_${RCON_CLI_VERSION}_linux_${archive_arch}.tar.gz"
    echo "${checksum}  ${archive}" | sha256sum --check --strict
    tar --extract --gzip --file "${archive}" --directory /usr/local/bin rcon-cli
    chmod 0755 /usr/local/bin/rcon-cli
    rm -f "${archive}"
}

create_environment() {
    install -d -m 0750 "${PZ_DATA_ROOT}" "${PZ_DATA_ROOT}/data" "${PZ_DATA_ROOT}/backups" /etc/pz-server /var/lib/pz-server
    install -d -o root -g root -m 0755 /var/lib/pz-deploy
    install -d -o 1000 -g 1000 -m 0750 "${PZ_DATA_ROOT}/backups/pre-update"
    chown 1000:1000 "${PZ_DATA_ROOT}/data" /var/lib/pz-server
    if [[ -e "${ENV_FILE}" ]]; then
        chmod 0600 "${ENV_FILE}"
        chown root:root "${ENV_FILE}"
        set_managed_environment_value RESTART_POLICY no
        log INFO "Preserving existing production secrets at ${ENV_FILE}."
        return
    fi

    umask 077
    cat > "${ENV_FILE}" <<EOF
COMPOSE_PROJECT_NAME=pz-server
PZ_SERVER_NAME=pzserver
SERVER_PUBLIC_NAME=Friends Project Zomboid Build 42
ADMIN_USERNAME=admin
ADMIN_PASSWORD=$(openssl rand -hex 32)
SERVER_PASSWORD=$(openssl rand -hex 24)
RCON_PASSWORD=$(openssl rand -hex 32)
MAX_PLAYERS=16
STEAM_APP_ID=380870
STEAM_BRANCH=
STEAM_BRANCH_PASSWORD=
PZ_UPDATE_POLICY=stable-on-start
ALLOW_AUTO_UPDATE_WITH_MODS=false
PZ_PRE_UPDATE_BACKUP_RETENTION=3
UPDATE_READINESS_TIMEOUT_SECONDS=1800
GAME_BIND_ADDRESS=0.0.0.0
GAME_PORT=16261
DIRECT_PORT=16262
RCON_PORT=27015
PZ_XMS=4g
PZ_XMX=8g
CONTAINER_CPUS=4.0
CONTAINER_MEMORY_LIMIT=14g
PZ_UID=1000
PZ_GID=1000
PZ_SERVER_PATH=/var/lib/pz-server
PZ_DATA_PATH=${PZ_DATA_ROOT}/data
PZ_PRE_UPDATE_BACKUP_PATH=${PZ_DATA_ROOT}/backups/pre-update
PZ_DEPLOY_STATE_PATH=/var/lib/pz-deploy
MODS=
WORKSHOP_ITEMS=
MAP_NAMES=Muldraugh, KY
RESTART_POLICY=no
SHUTDOWN_SAVE_SECONDS=15
SHUTDOWN_GRACE_SECONDS=90
TZ=UTC
IDLE_TIMEOUT_MINUTES=45
POLL_INTERVAL_SECONDS=60
RCON_RETRY_COUNT=3
RCON_RETRY_DELAY_SECONDS=5
RCON_TIMEOUT_SECONDS=10
HEALTH_FAILURE_TIMEOUT_MINUTES=12
FINAL_CHECK_COUNT=3
FINAL_CHECK_DELAY_SECONDS=5
SERVICE_STOP_TIMEOUT_SECONDS=120
DRY_RUN=false
DEPLOYMENT_ENVIRONMENT=aws
HOST_SHUTDOWN_ENABLED=true
HOST_SHUTDOWN_GUARD_FILE=/etc/pz-server/allow-host-shutdown
SERVICE_CONTROL_MODE=docker
COMPOSE_PROJECT_DIRECTORY=/opt/pz-stack
COMPOSE_ENV_FILE=${ENV_FILE}
COMPOSE_SERVICE=server
EOF
    chmod 0600 "${ENV_FILE}"
    chown root:root "${ENV_FILE}"
    log INFO "Generated production-local secrets at ${ENV_FILE} with mode 0600."
}

install_watchdog() {
    if [[ "${PRESERVE_WATCHDOG_VENV}" == true ]]; then
        [[ -x "${VENV_DIRECTORY}/bin/python" ]] \
            || die "PZ_PRESERVE_WATCHDOG_VENV=true but the previous environment is unavailable."
    elif [[ "${PRESERVE_WATCHDOG_VENV}" == false ]]; then
        python3 -m venv "${VENV_DIRECTORY}"
        timeout 10m "${VENV_DIRECTORY}/bin/pip" install --disable-pip-version-check --no-cache-dir "${PROJECT_DIRECTORY}"
    else
        die "PZ_PRESERVE_WATCHDOG_VENV must be true or false."
    fi
    install_file_atomically "${PROJECT_DIRECTORY}/scripts/linux/pzctl" /usr/local/bin/pzctl 0755
    install_file_atomically "${PROJECT_DIRECTORY}/scripts/linux/backup.sh" /usr/local/bin/pz-backup 0755
    install_file_atomically "${PROJECT_DIRECTORY}/scripts/linux/resize-data-volume.sh" /usr/local/sbin/pz-resize-data-volume 0755
    install_file_atomically "${PROJECT_DIRECTORY}/scripts/linux/deployment-unit-guard.sh" /usr/local/sbin/pz-deployment-unit-guard 0755
    install_file_atomically "${PROJECT_DIRECTORY}/scripts/linux/start-stack.sh" /usr/local/sbin/pz-start-stack 0755
}

install_services() {
    install_file_atomically "${PROJECT_DIRECTORY}/deploy/systemd/pz-stack.service" /etc/systemd/system/pz-stack.service 0644
    install_file_atomically "${PROJECT_DIRECTORY}/deploy/systemd/pz-watchdog.service" /etc/systemd/system/pz-watchdog.service 0644
    printf '%s\n' 'PZ_HOST_SHUTDOWN=ENABLED' > /etc/pz-server/allow-host-shutdown
    chmod 0600 /etc/pz-server/allow-host-shutdown
    systemctl daemon-reload
    systemctl enable pz-stack.service pz-watchdog.service
    systemctl restart pz-stack.service
    systemctl restart pz-watchdog.service
    sleep 5
    systemctl is-active --quiet pz-watchdog.service \
        || die "Project Zomboid watchdog did not remain active after installation."
}

main() {
    [[ $(id -u) -eq 0 ]] || die "Host installation must run as root."
    mountpoint --quiet "${PZ_DATA_ROOT}" || die "${PZ_DATA_ROOT} must be a mounted persistent filesystem."
    install_rcon_cli
    create_environment
    install_watchdog
    install_services
    log INFO "Project Zomboid host services installed and started."
}

main "$@"
