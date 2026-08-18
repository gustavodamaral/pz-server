#!/usr/bin/env bash
set -Eeuo pipefail

readonly DATA_ROOT=/srv/pz
readonly BACKUP_DIRECTORY="${DATA_ROOT}/backups"
readonly PROJECT_DIRECTORY=/opt/pz-stack
readonly ENV_FILE="${DATA_ROOT}/secrets.env"
readonly BACKUP_LOCK=/run/pz-server-backup.lock
readonly COMPOSE=(docker compose --project-directory "${PROJECT_DIRECTORY}" --file "${PROJECT_DIRECTORY}/compose.yaml" --env-file "${ENV_FILE}")
readonly RETENTION_COUNT="${BACKUP_RETENTION_COUNT:-7}"
allow_connected_players=false
was_running=false
watchdog_was_active=false
partial_archive=""
final_archive=""
backup_complete=false

log() {
    printf '%s %-7s %s\n' "$(date --utc +'%Y-%m-%dT%H:%M:%SZ')" "$1" "$2"
}

restart_if_needed() {
    local status=$?
    trap - EXIT
    if [[ -n "${partial_archive}" ]]; then
        rm -f -- "${partial_archive}"
    fi
    if [[ "${backup_complete}" != true && -n "${final_archive}" ]]; then
        rm -f -- "${final_archive}" "${final_archive}.sha256"
    fi
    flock --unlock 9 || true
    if [[ "${was_running}" == true ]]; then
        log INFO "Restarting Project Zomboid after backup attempt."
        if ! pzctl start; then
            log ERROR "Project Zomboid restart failed; manual intervention is required."
            status=1
        fi
    elif [[ "${watchdog_was_active}" == true ]]; then
        if ! systemctl start pz-watchdog.service; then
            log ERROR "Project Zomboid watchdog restore failed; manual intervention is required."
            status=1
        fi
    fi
    exit "${status}"
}

container_state() {
    docker inspect --format '{{json .State}}' "$1"
}

verify_quiescent() {
    local expected_identifier="$1"
    local current_identifier state
    current_identifier="$("${COMPOSE[@]}" ps --all --quiet server)"
    [[ "${current_identifier}" != *$'\n'* ]] \
        || { echo "Compose returned multiple server containers; backup is unsafe." >&2; return 1; }
    [[ "${current_identifier}" == "${expected_identifier}" ]] \
        || { echo "Server container identity changed during backup; discarding it." >&2; return 1; }
    if [[ -z "${expected_identifier}" ]]; then
        return 0
    fi
    state="$(container_state "${expected_identifier}")"
    jq --exit-status '.Running == false and .Restarting == false and .OOMKilled == false and .ExitCode == 0' \
        <<<"${state}" >/dev/null \
        || { echo "Server container is not in a verified clean stopped state." >&2; return 1; }
}

prune_archives() {
    local index=0 archive
    while IFS= read -r archive; do
        ((index += 1))
        if (( index > RETENTION_COUNT )); then
            rm -f -- "${archive}" "${archive}.sha256"
        fi
    done < <(
        find "${BACKUP_DIRECTORY}" -maxdepth 1 -type f -name 'pz-world-*.tar.gz' \
            -printf '%T@ %p\n' | sort --numeric-sort --reverse | cut --delimiter=' ' --fields=2-
    )
}

main() {
    [[ $(id -u) -eq 0 ]] || { echo "pz-backup requires root." >&2; exit 77; }
    if [[ "${1:-}" == --allow-connected-players ]]; then
        allow_connected_players=true
    elif [[ $# -gt 0 ]]; then
        echo "Usage: pz-backup [--allow-connected-players]" >&2
        exit 64
    fi

    if [[ ! "${RETENTION_COUNT}" =~ ^[0-9]+$ ]] \
        || (( RETENTION_COUNT < 1 || RETENTION_COUNT > 365 )); then
        echo "BACKUP_RETENTION_COUNT must be between 1 and 365." >&2
        exit 64
    fi
    install -d -m 0750 "${BACKUP_DIRECTORY}"
    exec 9>"${BACKUP_LOCK}"
    flock --exclusive --nonblock 9 \
        || { echo "Another Project Zomboid backup is already running." >&2; exit 75; }
    trap restart_if_needed EXIT
    if systemctl is-active --quiet pz-watchdog.service; then
        watchdog_was_active=true
        systemctl stop pz-watchdog.service
    fi

    local identifier state players timestamp archive archive_name
    identifier="$("${COMPOSE[@]}" ps --all --quiet server)"
    [[ "${identifier}" != *$'\n'* ]] \
        || { echo "Compose returned multiple server containers; backup is unsafe." >&2; exit 1; }
    if [[ -n "${identifier}" ]]; then
        state="$(container_state "${identifier}")"
        if jq --exit-status '.Restarting == true' <<<"${state}" >/dev/null; then
            echo "Server container is restarting; refusing backup." >&2
            exit 1
        fi
    fi

    if [[ -n "${identifier}" ]] && jq --exit-status '.Running == true' <<<"${state}" >/dev/null; then
        players="$(pzctl players 2>/dev/null || true)"
        if [[ ! "${players}" =~ ^[0-9]+$ ]]; then
            echo "Player state is UNKNOWN; refusing backup without exception." >&2
            exit 1
        fi
        if (( players > 0 )) && [[ "${allow_connected_players}" != true ]]; then
            echo "Refusing backup while ${players} player(s) are connected. Warn them before using --allow-connected-players." >&2
            exit 1
        fi
        was_running=true
        if [[ "${allow_connected_players}" == true ]]; then
            pzctl graceful-stop --allow-connected-players
        else
            pzctl graceful-stop
        fi
    elif [[ -n "${identifier}" ]]; then
        verify_quiescent "${identifier}"
    fi

    timestamp="$(date --utc +'%Y%m%dT%H%M%SZ')"
    archive_name="pz-world-${timestamp}.tar.gz"
    archive="${BACKUP_DIRECTORY}/${archive_name}"
    final_archive="${archive}"
    partial_archive="${BACKUP_DIRECTORY}/.${archive_name}.$$.partial"
    [[ ! -e "${archive}" ]] || { echo "Backup filename collision: ${archive}" >&2; exit 1; }
    log INFO "Creating consistent backup ${archive}."
    tar --create --gzip --file "${partial_archive}" --directory "${DATA_ROOT}" data
    verify_quiescent "${identifier}"
    mv -- "${partial_archive}" "${archive}"
    partial_archive=""
    (cd "${BACKUP_DIRECTORY}" && sha256sum "${archive_name}" > "${archive_name}.sha256")
    chmod 0640 "${archive}" "${archive}.sha256"
    prune_archives
    backup_complete=true
    log INFO "Backup complete. Copy it off this EBS volume for disaster recovery."
}

main "$@"
