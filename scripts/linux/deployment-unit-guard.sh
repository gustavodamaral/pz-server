#!/usr/bin/env bash
set -Eeuo pipefail

readonly PENDING_DEPLOYMENT_FILE=/var/lib/pz-deploy/deployment-pending
readonly START_AUTHORIZATION_FILE=/run/pz-deploy-start-authorized
readonly LIFECYCLE_LOCK=/run/pz-server-backup.lock

process_start_time() {
    local pid="$1"
    local stat_line fields
    [[ -r "/proc/${pid}/stat" ]] || return 1
    IFS= read -r stat_line < "/proc/${pid}/stat" || return 1
    fields="${stat_line##*) }"
    awk '{print $20}' <<<"${fields}"
}

authorization_is_valid() {
    local expected_pid="${1:-}"
    local expected_start_time="${2:-}"
    local authorization pid authorized_start_time actual_start_time
    [[ -f "${START_AUTHORIZATION_FILE}" ]] || return 1
    [[ "$(stat --format='%u:%a' "${START_AUTHORIZATION_FILE}")" == 0:600 ]] || return 1
    authorization="$(<"${START_AUTHORIZATION_FILE}")"
    [[ "${authorization}" =~ ^PZ_DEPLOY_START=([0-9]+):([0-9]+)$ ]] || return 1
    pid="${BASH_REMATCH[1]}"
    authorized_start_time="${BASH_REMATCH[2]}"
    [[ -z "${expected_pid}" || "${pid}" == "${expected_pid}" ]] || return 1
    [[ -z "${expected_start_time}" || "${authorized_start_time}" == "${expected_start_time}" ]] || return 1
    actual_start_time="$(process_start_time "${pid}")" || return 1
    [[ "${actual_start_time}" == "${authorized_start_time}" ]] || return 1
    [[ "$(readlink --canonicalize "/proc/${pid}/fd/8" 2>/dev/null || true)" == "${LIFECYCLE_LOCK}" ]]
}

monitor_deployment() {
    local expected_pid="${1:-}"
    local expected_start_time="${2:-}"
    [[ "${expected_pid}" =~ ^[0-9]+$ && "${expected_start_time}" =~ ^[0-9]+$ ]] || exit 64
    while [[ -e "${PENDING_DEPLOYMENT_FILE}" ]]; do
        if ! authorization_is_valid "${expected_pid}" "${expected_start_time}"; then
            rm -f -- "${START_AUTHORIZATION_FILE}"
            systemctl stop pz-watchdog.service pz-stack.service || true
            exit 1
        fi
        sleep 1
    done
}

if [[ "${1:-}" == monitor ]]; then
    shift
    monitor_deployment "$@"
    exit 0
fi
if [[ "${1:-}" == verify-authorization ]]; then
    [[ $# -eq 1 ]] || exit 64
    authorization_is_valid
    exit
fi
[[ $# -eq 0 ]] || exit 64
[[ ! -e "${PENDING_DEPLOYMENT_FILE}" ]] || authorization_is_valid
