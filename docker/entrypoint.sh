#!/usr/bin/env bash
set -Eeuo pipefail

readonly CONSOLE_FIFO=/tmp/pz-console
readonly PID_FILE=/tmp/pz-entrypoint.pid
readonly VERIFIED_START_SCRIPT_SHA256=9bfcb6a6367e4a6e833680ce09a373ee8ac930f1bd6fb1d8085bbe20cb725957
server_pid=""
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
    : "${UPDATE_ON_START:=false}"
    : "${STEAM_VALIDATE:=false}"
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
    require_integer SHUTDOWN_SAVE_SECONDS 0 30
    require_integer SHUTDOWN_GRACE_SECONDS 30 90
    [[ "${GAME_PORT}" != "${DIRECT_PORT}" ]] || die "GAME_PORT and DIRECT_PORT must differ."
    [[ "${RCON_PORT}" != "${GAME_PORT}" && "${RCON_PORT}" != "${DIRECT_PORT}" ]] || die "RCON_PORT must differ from gameplay ports."
    (( SHUTDOWN_SAVE_SECONDS + SHUTDOWN_GRACE_SECONDS <= 110 )) \
        || die "SHUTDOWN_SAVE_SECONDS plus SHUTDOWN_GRACE_SECONDS must not exceed 110."

    require_secret ADMIN_PASSWORD
    require_secret RCON_PASSWORD
    require_secret SERVER_PASSWORD
    [[ "${SERVER_PUBLIC_NAME}" != *$'\n'* && "${SERVER_PUBLIC_NAME}" != *$'\r'* ]] || die "SERVER_PUBLIC_NAME must be a single line."
}

prepare_directories() {
    mkdir --parents "${PZ_SERVER_DIR}" "${ZOMBOID_DIR}/Server" "${ZOMBOID_DIR}/Saves" "${ZOMBOID_DIR}/Logs" "${ZOMBOID_DIR}/db" "${ZOMBOID_DIR}/mods"
    [[ -w "${PZ_SERVER_DIR}" ]] || die "${PZ_SERVER_DIR} is not writable by UID $(id -u). Fix host ownership."
    [[ -w "${ZOMBOID_DIR}" ]] || die "${ZOMBOID_DIR} is not writable by UID $(id -u). Fix host ownership."
}

install_or_update_server() {
    if ! is_true "${UPDATE_ON_START}" && [[ -x "${PZ_SERVER_DIR}/start-server.sh" ]]; then
        log INFO "Skipping SteamCMD update because UPDATE_ON_START=false."
        return
    fi

    local -a command=(
        "${STEAMCMD_DIR}/steamcmd.sh"
        +force_install_dir "${PZ_SERVER_DIR}"
        +login anonymous
        +app_update "${STEAM_APP_ID}"
    )

    if [[ -n "${STEAM_BRANCH:-}" && "${STEAM_BRANCH,,}" != "stable" ]]; then
        command+=(-beta "${STEAM_BRANCH}")
        if [[ -n "${STEAM_BRANCH_PASSWORD:-}" ]]; then
            command+=(-betapassword "${STEAM_BRANCH_PASSWORD}")
        fi
    fi
    if is_true "${STEAM_VALIDATE}"; then
        command+=(validate)
    fi
    command+=(+quit)

    log INFO "Installing/updating Project Zomboid dedicated server App ${STEAM_APP_ID}, branch '${STEAM_BRANCH:-stable}'."
    "${command[@]}" || die "SteamCMD failed; persistent data was not removed."
    [[ -x "${PZ_SERVER_DIR}/start-server.sh" ]] || die "SteamCMD completed but start-server.sh is missing."
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

prepare_launcher_configuration() {
    local source="${PZ_SERVER_DIR}/ProjectZomboid64.json"
    local generated
    local xms_count xmx_count
    [[ -f "${source}" ]] || die "Missing current Build 42 launcher configuration: ${source}"
    xms_count="$(grep --extended-regexp --count -- '"-Xms[0-9]+[gGmM]"' "${source}" || true)"
    xmx_count="$(grep --extended-regexp --count -- '"-Xmx[0-9]+[gGmM]"' "${source}" || true)"
    [[ "${xmx_count}" == 1 && "${xms_count}" -le 1 ]] || die "Upstream ProjectZomboid64.json memory flags changed; refusing an unverified JVM allocation."
    generated="$(mktemp "${PZ_SERVER_DIR}/.ProjectZomboid64.json.XXXXXX")"

    if [[ "${xms_count}" == 1 ]]; then
        sed --regexp-extended \
            -e "s/\"-Xms[0-9]+[gGmM]\"/\"-Xms${PZ_XMS}\"/" \
            -e "s/\"-Xmx[0-9]+[gGmM]\"/\"-Xmx${PZ_XMX}\"/" \
            "${source}" > "${generated}"
    else
        sed --regexp-extended \
            -e "s/^([[:space:]]*)\"-Xmx[0-9]+[gGmM]\",/\1\"-Xms${PZ_XMS}\",\n\1\"-Xmx${PZ_XMX}\",/" \
            "${source}" > "${generated}"
    fi
    jq empty "${generated}" >/dev/null || die "Managed launcher configuration is not valid JSON."
    chmod --reference="${source}" "${generated}"
    mv "${generated}" "${source}"
    log INFO "Configured current Build 42 launcher heap as ${PZ_XMS}-${PZ_XMX}."
}

prepare_start_script() {
    local source="${PZ_SERVER_DIR}/start-server.sh"
    local destination="${PZ_SERVER_DIR}/.pz-start-server.sh"
    local generated checksum
    [[ -f "${source}" ]] || die "Missing current Build 42 start script: ${source}"
    checksum="$(sha256sum "${source}" | cut --delimiter=' ' --fields=1)"
    [[ "${checksum}" == "${VERIFIED_START_SCRIPT_SHA256}" ]] \
        || die "Upstream start-server.sh changed; refusing an unreviewed process-status wrapper."
    generated="$(mktemp "${PZ_SERVER_DIR}/.pz-start-server.sh.XXXXXX")"
    if ! sed \
        -e 's/^[[:space:]]*echo "Only 64bit is supported"$/\techo "Only 64bit is supported" >\&2; exit 1/' \
        -e 's/^exit 0$/exit $?/' \
        "${source}" > "${generated}"; then
        rm -f -- "${generated}"
        die "Could not generate the verified process-status wrapper."
    fi
    if ! chmod --reference="${source}" "${generated}" || ! mv "${generated}" "${destination}"; then
        rm -f -- "${generated}"
        die "Could not install the verified process-status wrapper."
    fi
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
    local start_script="$1"
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
    cd "${PZ_SERVER_DIR}"
    setsid bash "${start_script}" "${arguments[@]}" <&3 &
    server_pid=$!
    printf '%s\n' "${server_pid}" > "${PID_FILE}"

    local status=0 final_status
    wait "${server_pid}" || status=$?
    if [[ "${shutdown_requested}" == true ]]; then
        graceful_shutdown
        final_status=0
        wait "${server_pid}" || final_status=$?
        # 127 means the first wait already reaped the child; retain that status.
        if (( final_status != 127 )); then
            status="${final_status}"
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
    install_or_update_server
    render_configuration
    prepare_launcher_configuration
    prepare_start_script
    run_server "${PZ_SERVER_DIR}/.pz-start-server.sh"
}

main "$@"
