#!/usr/bin/env bash
# free_kib in install.sh must print the KiB available to a non-root user. It
# uses stat -f, which GNU and BusyBox both support. df --output is GNU-only:
# BusyBox df rejects it, so the installer refused every BusyBox host.
# The stubs mirror a BusyBox df (rejects --output) and a stat -f that reports
# 4693801 available blocks of 4096 bytes (18775204 KiB, the value the real
# host reports).
#
# Run: bash tests/install_free_space_test.sh
set -u
repo=$(cd "$(dirname "$0")/.." && pwd)
work=$(mktemp -d "${TMPDIR:-/tmp}/ergosphere-free-space-test.XXXXXX")
trap 'rm -rf -- "$work"' EXIT

mkdir -p "$work/bin"
printf '#!/bin/sh\necho "df: unrecognized option" >&2\nexit 1\n' >"$work/bin/df"
printf '#!/bin/sh\necho "4693801 4096"\n' >"$work/bin/stat"
chmod +x "$work/bin/df" "$work/bin/stat"
sed -n '/^free_kib()/,/^}/p' "$repo/install.sh" >"$work/fn.sh"

got=$(PATH="$work/bin:$PATH" bash -c 'source "$1"; free_kib /mnt/x' _ "$work/fn.sh")
if [[ "$got" == 18775204 ]]; then
  echo "ok:   free_kib reports available KiB without df --output"
  exit 0
fi
echo "FAIL: free_kib printed '$got', want 18775204"
exit 1
