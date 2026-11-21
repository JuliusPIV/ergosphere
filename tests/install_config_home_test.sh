#!/usr/bin/env bash
# install.sh resolves a leading ~ or ~user in ERGOSPHERE_CONFIG the way the
# server does (pathlib expanduser), and refuses a ~user it cannot resolve.
#
# Run: ERGOSPHERE_TEST_PORT=<spare port> bash tests/install_config_home_test.sh
# --check only: nothing is installed or written outside a throwaway dir.
# The release download points at a closed local port, so --check stops at
# download; the config rows it checks are printed before that. The port is
# only probed.
# shellcheck disable=SC2088  # a literal ~ is the input under test
set -u
repo=$(cd "$(dirname "$0")/.." && pwd)
port=${ERGOSPHERE_TEST_PORT:-9550}
work=$(mktemp -d "${TMPDIR:-/tmp}/ergosphere-home-test.XXXXXX")
trap 'rm -rf -- "$work"' EXIT
fail=0
ok() { echo "ok:   $1"; }
bad() { echo "FAIL: $1"; fail=1; }

mkdir -p "$work/home" "$work/bin"
printf '#!/bin/sh\necho "grok 0.0.0-test"\n' >"$work/bin/grok"
chmod +x "$work/bin/grok"
me=$(id -un)
leaf=".ergosphere-home-test-$$/settings.json"

# check RAW [DIR]: prints the installer --check output for ERGOSPHERE_CONFIG=RAW.
check() {
  env -u ERGOSPHERE_PORT HOME="$work/home" PATH="$work/bin:$PATH" TMPDIR="$work" \
    ERGOSPHERE_CONFIG="$1" ERGOSPHERE_INSTALL_SOURCE_URL=http://127.0.0.1:9/unused \
    bash "$repo/install.sh" --check --dir "${2:-$work/app}" --port "$port" </dev/null 2>&1
}

# What the server reads for the same value and HOME.
server_path() {
  HOME="$work/home" python3 -I -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).expanduser())' "$1"
}

for raw in "~/$leaf" "~$me/$leaf"; do
  want=$(server_path "$raw")
  # A decoy where the old code looked: the install dir plus the raw value.
  mkdir -p "$(dirname "$work/app/$raw")"
  printf '{}\n' >"$work/app/$raw"
  out=$(check "$raw")
  row=$(grep -m1 '^  [a-z]* *config ' <<<"$out")
  if [[ "$row" == *" $want "* ]]; then
    ok "$raw: installer uses $want, as the server does"
  else
    bad "$raw: config row '$row', want $want"
  fi
done

# A trailing newline is part of the name for the server, so the file without
# it must not be the one the installer reads.
mkdir -p "$work/home/${leaf%/*}"
printf '{}\n' >"$work/home/$leaf"
out=$(check "~/$leaf"$'\n' "$work/app3")
if [[ "$out" != *"$work/home/$leaf present"* ]]; then
  ok "~/$leaf<newline>: the file without the newline is not read"
else
  bad "~/$leaf<newline>: installer read $work/home/$leaf"
fi

raw="~no-such-user-$$/$leaf"
out=$(check "$raw" "$work/app2")
rc=$?
if [[ "$rc" -ne 0 && "$out" == *"ERGOSPHERE_CONFIG"* && "$out" != *"ready:"* ]]; then
  ok "$raw: refused (rc $rc)"
else
  bad "$raw: rc $rc, want a refusal naming ERGOSPHERE_CONFIG"
  printf '%s\n' "$out" | tail -n 5
fi

exit "$fail"
