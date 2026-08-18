#!/usr/bin/env bash
set -Eeuo pipefail

if ! pgrep --full '([z]ombie\.network\.GameServer|[P]rojectZomboid64.*-servername)' >/dev/null; then
    echo "Project Zomboid server process is not running." >&2
    exit 1
fi
