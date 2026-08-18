#!/usr/bin/env bash
set -Eeuo pipefail

readonly fifo=/tmp/pz-console
[[ $# -gt 0 ]] || { echo "Usage: pz-console <command> [arguments...]" >&2; exit 64; }
[[ -p "${fifo}" ]] || { echo "Project Zomboid console FIFO is unavailable." >&2; exit 1; }
printf '%s\n' "$*" > "${fifo}"
