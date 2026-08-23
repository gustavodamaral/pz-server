#!/usr/bin/env bash
set -Eeuo pipefail

readonly CONSOLE_FIFO=/tmp/pz-console
readonly PID_FILE=/tmp/pz-entrypoint.pid
server_pid=""
readiness_pid=""
selected_release=""
shutdown_requested=false
shutdown_failed=false

log() {
    local level="$1"
    shift
    printf '%s %-7s %s\n' "$(date --utc +'%Y-%m-%dT%H:%M:%SZ')" "${level}" "$*"
}

die() {
    log ERROR "$*"
    exit 1
}

is_true() {
    case "${1,,}" in
        1|true|yes|on) return 0 ;;
        0|false|no|off) return 1 ;;
        *) die "Expected a boolean value, got '${1}'." ;;
    esac
}

require_secret() {
    local name="$1"
    local value="${!name:-}"
    [[ -n "${value}" ]] || die "${name} must not be empty."
    [[ "${value}" != change-me-* ]] || die "${name} still contains the unsafe example placeholder."
    [[ "${value}" != *$'\n'* && "${value}" != *$'\r'* ]] || die "${name} must be a single line."
}

require_integer() {
    local name="$1"
    local minimum="$2"
    local maximum="$3"
    local value="${!name}"
    [[ "${value}" =~ ^[0-9]+$ ]] || die "${name} must be an integer."
    (( value >= minimum && value <= maximum )) || die "${name} must be between ${minimum} and ${maximum}."
}

memory_mib() {
    local value="${1,,}"
    local number="${value%?}"
    case "${value: -1}" in
        g) printf '%s\n' "$((number * 1024))" ;;
        m) printf '%s\n' "${number}" ;;
        *) return 1 ;;
    esac
}

validate_environment() {
    local container_limit_mib xms_mib xmx_mib
    : "${PZ_SERVER_NAME:=pzserver}"
    : "${SERVER_PUBLIC_NAME:=Friends Project Zomboid Build 42}"
    : "${ADMIN_USERNAME:=admin}"
    : "${MAX_PLAYERS:=16}"
    : "${GAME_PORT:=16261}"
    : "${DIRECT_PORT:=16262}"
    : "${RCON_PORT:=27015}"
    : "${STEAM_APP_ID:=380870}"
    : "${PZ_UPDATE_POLICY:=stable-on-start}"
    : "${ALLOW_AUTO_UPDATE_WITH_MODS:=false}"
    : "${PZ_PRE_UPDATE_BACKUP_RETENTION:=3}"
    : "${PZ_BACKUP_DIR:=/backups}"
    : "${UPDATE_READINESS_TIMEOUT_SECONDS:=1800}"
    : "${DEPLOYMENT_ENVIRONMENT:=local}"
    : "${PZ_XMS:=2g}"
    : "${PZ_XMX:=8g}"
    : "${CONTAINER_MEMORY_LIMIT:=10g}"
    : "${SHUTDOWN_SAVE_SECONDS:=15}"
    : "${SHUTDOWN_GRACE_SECONDS:=90}"
    : "${MODS:=}"
    : "${WORKSHOP_ITEMS:=}"
    : "${MAP_NAMES:=Muldraugh, KY}"

    [[ "${PZ_SERVER_NAME}" =~ ^[A-Za-z0-9_-]+$ ]] || die "PZ_SERVER_NAME may contain only letters, numbers, underscores, and hyphens."
    [[ "${ADMIN_USERNAME}" =~ ^[A-Za-z0-9_-]+$ ]] || die "ADMIN_USERNAME may contain only letters, numbers, underscores, and hyphens."
    [[ "${PZ_XMS}" =~ ^[1-9][0-9]*[gGmM]$ ]] || die "PZ_XMS must look like 2g or 2048m."
    [[ "${PZ_XMX}" =~ ^[1-9][0-9]*[gGmM]$ ]] || die "PZ_XMX must look like 8g or 8192m."
    [[ "${CONTAINER_MEMORY_LIMIT}" =~ ^[1-9][0-9]*[gGmM]$ ]] \
        || die "CONTAINER_MEMORY_LIMIT must look like 10g or 10240m."
    xms_mib="$(memory_mib "${PZ_XMS}")"
    xmx_mib="$(memory_mib "${PZ_XMX}")"
    container_limit_mib="$(memory_mib "${CONTAINER_MEMORY_LIMIT}")"
    (( xms_mib <= xmx_mib )) || die "PZ_XMS must not exceed PZ_XMX."
    (( xmx_mib < container_limit_mib )) \
        || die "PZ_XMX must remain below CONTAINER_MEMORY_LIMIT for native/off-heap memory."

    require_integer MAX_PLAYERS 1 100
    require_integer GAME_PORT 1024 65535
    require_integer DIRECT_PORT 1024 65535
    require_integer RCON_PORT 1024 65535
    require_integer STEAM_APP_ID 1 2147483647
    require_integer PZ_PRE_UPDATE_BACKUP_RETENTION 2 20
    require_integer UPDATE_READINESS_TIMEOUT_SECONDS 30 3600
    require_integer SHUTDOWN_SAVE_SECONDS 0 30
    require_integer SHUTDOWN_GRACE_SECONDS 30 90
    [[ "${GAME_PORT}" != "${DIRECT_PORT}" ]] || die "GAME_PORT and DIRECT_PORT must differ."
    [[ "${RCON_PORT}" != "${GAME_PORT}" && "${RCON_PORT}" != "${DIRECT_PORT}" ]] || die "RCON_PORT must differ from gameplay ports."
    (( SHUTDOWN_SAVE_SECONDS + SHUTDOWN_GRACE_SECONDS <= 110 )) \
        || die "SHUTDOWN_SAVE_SECONDS plus SHUTDOWN_GRACE_SECONDS must not exceed 110."
    [[ "${STEAM_APP_ID}" == 380870 ]] || die "Only dedicated-server Steam App 380870 is supported."
    case "${PZ_UPDATE_POLICY}" in
        stable-on-start|manual) ;;
        *) die "PZ_UPDATE_POLICY must be stable-on-start or manual." ;;
    esac
    [[ "${DEPLOYMENT_ENVIRONMENT}" == local || "${DEPLOYMENT_ENVIRONMENT}" == aws ]] \
        || die "DEPLOYMENT_ENVIRONMENT must be local or aws."
    is_true "${ALLOW_AUTO_UPDATE_WITH_MODS}" || true

    require_secret ADMIN_PASSWORD
    require_secret RCON_PASSWORD
    require_secret SERVER_PASSWORD
    [[ "${SERVER_PUBLIC_NAME}" != *$'\n'* && "${SERVER_PUBLIC_NAME}" != *$'\r'* ]] || die "SERVER_PUBLIC_NAME must be a single line."
}

prepare_directories() {
    mkdir --parents "${PZ_SERVER_DIR}" "${PZ_BACKUP_DIR}" "${ZOMBOID_DIR}/Server" "${ZOMBOID_DIR}/Saves" "${ZOMBOID_DIR}/Logs" "${ZOMBOID_DIR}/db" "${ZOMBOID_DIR}/mods"
    [[ -w "${PZ_SERVER_DIR}" ]] || die "${PZ_SERVER_DIR} is not writable by UID $(id -u). Fix host ownership."
    [[ -w "${ZOMBOID_DIR}" ]] || die "${ZOMBOID_DIR} is not writable by UID $(id -u). Fix host ownership."
    [[ -w "${PZ_BACKUP_DIR}" ]] || die "${PZ_BACKUP_DIR} is not writable by UID $(id -u). Fix host ownership."
}

prepare_release() {
    if ! pz-updater prepare-start >&2; then
        die "Project Zomboid release preparation failed; the world was not started."
    fi
    selected_release="$(pz-updater active-release)" \
        || die "Project Zomboid active release lookup failed."
    [[ -n "${selected_release}" && "${selected_release}" != *$'\n'* ]] \
        || die "Updater returned an ambiguous active release path."
    case "${selected_release}" in
        "${PZ_SERVER_DIR}"/releases/*) ;;
        *) die "Updater selected a release outside ${PZ_SERVER_DIR}." ;;
    esac
    [[ -x "${selected_release}/.pz-start-server.sh" ]] \
        || die "Selected release has no executable managed start wrapper."
}

merge_managed_ini() {
    local rendered="$1"
    local destination="$2"
    local merged="${destination}.merged"

    if [[ ! -f "${destination}" ]]; then
        install -m 0600 "${rendered}" "${destination}"
        return
    fi

    awk -F= '
        NR == FNR {
            if ($0 !~ /^[[:space:]]*[#;]/ && index($0, "=") > 0) {
                key = $1
                gsub(/^[[:space:]]+|[[:space:]]+$/, "", key)
                managed[key] = $0
                order[++count] = key
            }
            next
        }
        {
            line = $0
            key = $1
            gsub(/^[[:space:]]+|[[:space:]]+$/, "", key)
            if (index(line, "=") > 0 && key in managed) {
                if (!(key in emitted)) {
                    print managed[key]
                    emitted[key] = 1
                }
            } else {
                print line
            }
        }
        END {
            for (i = 1; i <= count; i++) {
                key = order[i]
                if (!(key in emitted)) {
                    print managed[key]
                }
            }
        }
    ' "${rendered}" "${destination}" > "${merged}"
    chmod 0600 "${merged}"
    mv "${merged}" "${destination}"
}

render_configuration() {
    local template=/config/server.ini.template
    local rendered
    local destination="${ZOMBOID_DIR}/Server/${PZ_SERVER_NAME}.ini"
    [[ -r "${template}" ]] || die "Missing configuration template: ${template}"
    rendered="$(mktemp)"

    # The single-quoted expression is an intentional envsubst variable allowlist.
    # shellcheck disable=SC2016
    envsubst '${SERVER_PUBLIC_NAME} ${SERVER_PASSWORD} ${MAX_PLAYERS} ${GAME_PORT} ${DIRECT_PORT} ${RCON_PORT} ${RCON_PASSWORD} ${MODS} ${WORKSHOP_ITEMS} ${MAP_NAMES}' \
        < "${template}" > "${rendered}"
    merge_managed_ini "${rendered}" "${destination}"
    rm -f "${rendered}"
    log INFO "Merged managed settings into ${destination}; generated identity and unmanaged settings were preserved."
}

request_shutdown() {
    shutdown_requested=true
    trap - SIGTERM SIGINT
}

graceful_shutdown() {
    local deadline
    [[ -n "${server_pid}" ]] || return 0
    if ! kill -0 "${server_pid}" 2>/dev/null; then
        return 0
    fi

    log INFO "Shutdown signal received; requesting a world save."
    if ! printf 'save\n' >&3; then
        log ERROR "Could not write save command to the server console."
        shutdown_failed=true
    fi
    sleep "${SHUTDOWN_SAVE_SECONDS}"
    log INFO "Requesting graceful Project Zomboid quit."
    if ! printf 'quit\n' >&3; then
        log ERROR "Could not write quit command to the server console."
        shutdown_failed=true
    fi

    deadline=$((SECONDS + SHUTDOWN_GRACE_SECONDS))
    while kill -0 "${server_pid}" 2>/dev/null && (( SECONDS < deadline )); do
        sleep 1
    done
    if kill -0 "${server_pid}" 2>/dev/null; then
        log ERROR "Project Zomboid did not stop within ${SHUTDOWN_GRACE_SECONDS}s; sending SIGTERM to its process group."
        shutdown_failed=true
        kill -TERM -- "-${server_pid}" 2>/dev/null || true
        sleep 5
    fi
    if kill -0 "${server_pid}" 2>/dev/null; then
        log ERROR "Project Zomboid still did not stop; sending SIGKILL to prevent a hung container."
        kill -KILL -- "-${server_pid}" 2>/dev/null || true
    fi
}

run_server() {
    local release="$1"
    local start_script="${release}/.pz-start-server.sh"
    local runtime_log="${ZOMBOID_DIR}/server-console.txt"
    local log_fingerprint
    local -a arguments=(-servername "${PZ_SERVER_NAME}")
    local database="${ZOMBOID_DIR}/db/${PZ_SERVER_NAME}.db"

    if [[ ! -s "${database}" ]]; then
        log INFO "No server database exists yet; supplying the initial admin credentials."
        arguments+=(-adminusername "${ADMIN_USERNAME}" -adminpassword "${ADMIN_PASSWORD}")
    fi

    rm -f "${CONSOLE_FIFO}"
    mkfifo --mode=0600 "${CONSOLE_FIFO}"
    exec 3<>"${CONSOLE_FIFO}"
    trap request_shutdown SIGTERM SIGINT

    log INFO "Starting Project Zomboid '${PZ_SERVER_NAME}' with heap ${PZ_XMS}-${PZ_XMX}. PauseEmpty is managed as true."
    log_fingerprint="$(pz-updater log-fingerprint --log "${runtime_log}")" \
        || die "Could not fingerprint the Project Zomboid runtime log."
    pz-updater mark-world-opened >/dev/null \
        || die "Could not record the candidate world-open boundary."
    cd "${release}"
    setsid bash "${start_script}" "${arguments[@]}" <&3 &
    server_pid=$!
    printf '%s\n' "${server_pid}" > "${PID_FILE}"
    pz-updater wait-runtime-ready \
        --pid "${server_pid}" \
        --log "${runtime_log}" \
        --log-fingerprint "${log_fingerprint}" \
        --timeout "${UPDATE_READINESS_TIMEOUT_SECONDS}" &
    readiness_pid=$!

    local status=0 final_status completed_pid="" readiness_failed=false
    while [[ -n "${server_pid}" && -n "${readiness_pid}" ]]; do
        completed_pid=""
        if wait -n -p completed_pid "${server_pid}" "${readiness_pid}"; then
            status=0
        else
            status=$?
        fi
        if [[ "${shutdown_requested}" == true ]]; then
            graceful_shutdown
            final_status=0
            wait "${server_pid}" || final_status=$?
            if (( final_status != 127 )); then
                status="${final_status}"
            fi
            kill -TERM "${readiness_pid}" 2>/dev/null || true
            wait "${readiness_pid}" 2>/dev/null || true
            readiness_pid=""
            break
        fi
        if [[ "${completed_pid}" == "${readiness_pid}" ]]; then
            readiness_pid=""
            if (( status != 0 )); then
                log ERROR "Runtime readiness failed; stopping the current candidate without rollback."
                readiness_failed=true
                graceful_shutdown
                final_status=0
                wait "${server_pid}" || final_status=$?
                (( final_status != 127 && final_status != 0 )) && status="${final_status}"
                (( status == 0 )) && status=1
            fi
            break
        fi
        if [[ "${completed_pid}" == "${server_pid}" ]]; then
            server_pid=""
            kill -TERM "${readiness_pid}" 2>/dev/null || true
            wait "${readiness_pid}" 2>/dev/null || true
            readiness_pid=""
            pz-updater fail-readiness --detail "Project Zomboid exited before runtime readiness" >/dev/null \
                || log ERROR "Could not record the pre-readiness server exit."
            break
        fi
    done
    if [[ -n "${server_pid}" && "${shutdown_requested}" != true ]]; then
        wait "${server_pid}" || status=$?
    fi
    if [[ "${shutdown_requested}" == true && -n "${server_pid}" ]]; then
        graceful_shutdown
        final_status=0
        wait "${server_pid}" || final_status=$?
        if (( final_status != 127 )); then
            status="${final_status}"
        fi
        if [[ -n "${readiness_pid}" ]]; then
            kill -TERM "${readiness_pid}" 2>/dev/null || true
            wait "${readiness_pid}" 2>/dev/null || true
            readiness_pid=""
        fi
    fi
    rm -f "${PID_FILE}" "${CONSOLE_FIFO}"
    exec 3>&-

    if [[ "${shutdown_requested}" == true ]]; then
        if [[ "${shutdown_failed}" == true ]] || (( status != 0 )); then
            log ERROR "Project Zomboid stopped only after an unverified or escalated shutdown."
            (( status != 0 )) && return "${status}"
            return 1
        fi
        log INFO "Project Zomboid stopped after a graceful shutdown request."
        return 0
    fi
    if [[ "${readiness_failed}" == true ]]; then
        log ERROR "Project Zomboid was stopped because runtime readiness failed."
        return "${status}"
    fi
    if (( status != 0 )); then
        log ERROR "Project Zomboid exited unexpectedly with status ${status}."
    else
        log INFO "Project Zomboid exited."
    fi
    return "${status}"
}

main() {
    validate_environment
    prepare_directories
    render_configuration
    prepare_release
    run_server "${selected_release}"
}

main "$@"
