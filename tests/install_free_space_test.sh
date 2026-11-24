#!/usr/bin/env bash
# free_kib in install.sh must report the Available column even when the
# filesystem name has a space (df -P prints the name in column 1, so a name
# with a space shifts every later column; the old parse read Used instead).
# The df stub is GNU-like: --output=avail prints the Avail column, and the
# -P form prints a row whose name has a space.
#
# Run: bash tests/install_free_space_test.sh
set -u
repo=$(cd "$(dirname "$0")/.." && pwd)
work=$(mktemp -d "${TMPDIR:-/tmp}/ergosphere-free-space-test.XXXXXX")
trap 'rm -rf -- "$work"' EXIT

mkdir -p "$work/dfbin"
printf '#!/bin/sh\nfor a in "$@"; do\n  if [ "$a" = --output=avail ]; then\n    echo Avail; echo 900000; exit 0\n  fi\ndone\necho "Filesystem 1024-blocks Used Available Capacity Mounted on"\necho "my share 1000000 100 900000 1%% /mnt/x"\n' >"$work/dfbin/df"
chmod +x "$work/dfbin/df"
sed -n '/^free_kib()/,/^}/p' "$repo/install.sh" >"$work/fn.sh"

got=$(PATH="$work/dfbin:$PATH" bash -c 'source "$1"; free_kib /mnt/x' _ "$work/fn.sh")
if [[ "$got" == 900000 ]]; then
  echo "ok:   free_kib reads Available with a space in the filesystem name"
  exit 0
fi
echo "FAIL: free_kib printed '$got', want 900000 (Available)"
exit 1
