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
if [[ "$got" != 18775204 ]]; then
  echo "FAIL: free_kib printed '$got', want 18775204"
  exit 1
fi
echo "ok:   free_kib reports available KiB without df --output"

# The installer's prerequisites do not list awk, so a host without it must
# still get the free space. PATH holds only the stat stub here.
bash_bin=$(command -v bash)
got=$(PATH="$work/bin" "$bash_bin" -c 'source "$1"; free_kib /mnt/x' _ "$work/fn.sh")
if [[ "$got" != 18775204 ]]; then
  echo "FAIL: free_kib with no awk on PATH printed '$got', want 18775204"
  exit 1
fi
echo "ok:   free_kib works with no awk on PATH"
exit 0
