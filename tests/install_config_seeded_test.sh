#!/usr/bin/env bash
# install.sh accepts a new install dir that holds only the file
# ERGOSPHERE_CONFIG names (and the folders leading to it), and still refuses
# a dir that holds anything else.
#
# Run: ERGOSPHERE_TEST_PORT=<spare port> bash tests/install_config_seeded_test.sh
# --check only: nothing is downloaded, installed or written outside a
# throwaway dir. The port is only probed.
set -u
repo=$(cd "$(dirname "$0")/.." && pwd)
port=${ERGOSPHERE_TEST_PORT:-9550}
work=$(mktemp -d "${TMPDIR:-/tmp}/ergosphere-seeded-test.XXXXXX")
trap 'rm -rf -- "$work"' EXIT
fail=0
ok() { echo "ok:   $1"; }
bad() { echo "FAIL: $1"; fail=1; }

mkdir -p "$work/home" "$work/bin"
printf '#!/bin/sh\necho "grok 0.0.0-test"\n' >"$work/bin/grok"
chmod +x "$work/bin/grok"

# check DIR CONFIG: prints the installer --check output and its exit code.
check() {
  env -u ERGOSPHERE_PORT HOME="$work/home" PATH="$work/bin:$PATH" TMPDIR="$work" \
    ERGOSPHERE_CONFIG="$2" ERGOSPHERE_INSTALL_SOURCE_URL=http://127.0.0.1:9/unused \
    bash "$repo/install.sh" --check --dir "$1" --port "$port" </dev/null 2>&1
  echo "rc=$?"
}

# expect NAME accept|refuse DIR CONFIG
expect() {
  local name=$1 want=$2 out
  out=$(check "$3" "$4")
  if [[ "$want" == accept ]]; then
    if [[ "$out" == *"ready:"* && "$out" == *"rc=0" ]]; then ok "$name: accepted"; else
      bad "$name: refused, want accepted"; printf '%s\n' "$out" | tail -n 6; fi
  else
    if [[ "$out" == *"is not empty and is not an Ergosphere install"* && "$out" != *"rc=0" ]]; then ok "$name: refused"; else
      bad "$name: not refused as not empty"; printf '%s\n' "$out" | tail -n 6; fi
  fi
}

# Seeded with the test port, so no check falls back to the default port.
seed() { mkdir -p "${1%/*}"; printf '{"port": %s}\n' "$port" >"$1"; }

seed "$work/a/conf/e.json"
expect "only the config (relative)" accept "$work/a" conf/e.json

seed "$work/b/e.json"
expect "only the config at the top" accept "$work/b" e.json

seed "$work/c/conf/e.json"
expect "only the config (absolute, inside the dir)" accept "$work/c" "$work/c/conf/e.json"

seed "$work/g/conf/e.json"
expect "only the config (written ./conf//e.json)" accept "$work/g" ./conf//e.json

seed "$work/d/conf/e.json"
seed "$work/d/conf/other.txt"
expect "config plus another file beside it" refuse "$work/d" conf/e.json

seed "$work/e/conf/e.json"
mkdir -p "$work/e/stray"
expect "config plus an empty folder" refuse "$work/e" conf/e.json

seed "$work/f/notes.txt"
expect "a stray file, config not there" refuse "$work/f" conf/e.json

exit "$fail"
