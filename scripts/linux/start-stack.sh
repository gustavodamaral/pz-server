#!/usr/bin/env bash
set -Eeuo pipefail

readonly PROJECT_DIRECTORY=/opt/pz-stack
readonly ENV_FILE=/srv/pz/secrets.env
readonly LIFECYCLE_LOCK=/run/pz-server-backup.lock
readonly START_TIMEOUT_SECONDS=3600
readonly COMPOSE=(docker compose --project-directory "${PROJECT_DIRECTORY}" --file "${PROJECT_DIRECTORY}/compose.yaml" --env-file "${ENV_FILE}")

cancel_start() {
    echo "Startup cancellation requested; stopping the current Compose server." >&2
    "${COMPOSE[@]}" stop --timeout 120 server || true
    exit 143
}

trap cancel_start INT TERM

exec 9>"${LIFECYCLE_LOCK}"
if flock --exclusive --nonblock 9; then
    lock_source=direct
elif /usr/local/sbin/pz-deployment-unit-guard verify-authorization; then
    lock_source=authorized-pzctl
else
    echo "Project Zomboid lifecycle lock is held without valid start authorization." >&2
    exit 75
fi

echo "Starting Project Zomboid stack with lifecycle lock source ${lock_source}."
"${COMPOSE[@]}" up \
    --detach \
    --build \
    --wait \
    --wait-timeout "${START_TIMEOUT_SECONDS}" \
    server
