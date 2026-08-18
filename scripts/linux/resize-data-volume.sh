#!/usr/bin/env bash
set -Eeuo pipefail

readonly DATA_MOUNT=/srv/pz

die() {
    printf 'pz-storage-resize: %s\n' "$*" >&2
    exit 1
}

[[ $(id -u) -eq 0 ]] || die "root is required."
mountpoint --quiet "${DATA_MOUNT}" || die "${DATA_MOUNT} is not mounted."

filesystem="$(findmnt --noheadings --output FSTYPE --target "${DATA_MOUNT}")"
device="$(findmnt --noheadings --output SOURCE --target "${DATA_MOUNT}")"
[[ "${filesystem}" == ext4 ]] || die "expected ext4, found '${filesystem}'."
[[ -b "${device}" ]] || die "mount source '${device}' is not a block device."

# resize2fs is idempotent and supports growing a mounted ext4 filesystem.
resize2fs "${device}"
