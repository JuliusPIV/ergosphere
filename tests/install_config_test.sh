#!/usr/bin/env bash
# install.sh reads, writes and reports the ERGOSPHERE_CONFIG file when it is
# set, and ergosphere.json next to ergosphere.py when it is not. Each case
# installs into a throwaway HOME, then reruns to show nothing changes.
#
# Run: ERGOSPHERE_TEST_PORT=<spare port> bash tests/install_config_test.sh
# No network: the package is served from 127.0.0.1 and grok is a stub that
# only answers --version. Every server it starts is stopped at the end.
set -u
repo=$(cd "$(dirname "$0")/.." && pwd)
port=${ERGOSPHERE_TEST_PORT:-9550}
work=$(mktemp -d "${TMPDIR:-/tmp}/ergosphere-install-test.XXXXXX")
pids=()
fail=0

# shellcheck disable=SC2317  # run by the EXIT trap
cleanup() {
  local p
  for p in ${pids[@]+"${pids[@]}"}; do kill "$p" 2>/dev/null; done
  rm -rf -- "$work"
}
trap cleanup EXIT

ok() { echo "ok:   $1"; }
bad() { echo "FAIL: $1"; fail=1; }

# Package: one top folder holding the runtime, as the release archive does.
mkdir -p "$work/pkg/ergosphere-test" "$work/bin" "$work/www"
cp -p "$repo/ergosphere.py" "$repo/LICENSE" "$work/pkg/ergosphere-test/"
cp -rp "$repo/vendor" "$work/pkg/ergosphere-test/"
tar -czf "$work/www/src.tar.gz" -C "$work/pkg" ergosphere-test
printf '#!/bin/sh\necho "grok 0.0.0-test"\n' >"$work/bin/grok"
chmod +x "$work/bin/grok"

python3 -I -c '
import functools, http.server, sys
h = functools.partial(http.server.SimpleHTTPRequestHandler, directory=sys.argv[1])
h.log_message = lambda *a: None
s = http.server.HTTPServer(("127.0.0.1", 0), h)
open(sys.argv[2], "w").write(str(s.server_address[1]))
s.serve_forever()' "$work/www" "$work/www.port" </dev/null >/dev/null 2>&1 &
pids+=($!)
for _ in $(seq 50); do [[ -s "$work/www.port" ]] && break; sleep 0.1; done
url="http://127.0.0.1:$(cat "$work/www.port")/src.tar.gz"

# run_install HOME [installer args...]: prints the installer output.
run_install() {
  local home=$1
  shift
  env -u ERGOSPHERE_PORT HOME="$home" PATH="$work/bin:$PATH" TMPDIR="$work" \
    ERGOSPHERE_INSTALL_SOURCE_URL="$url" bash "$repo/install.sh" --auto "$@" </dev/null 2>&1
}

field() { sed -n "s/^  $1: *//p" <<<"$2" | head -n 1; }

# check_case NAME HOME CONFIG_FILE: install, check, rerun, stop the server.
check_case() {
  local name=$1 home=$2 cfg=$3 out pid saved
  out=$(run_install "$home" --dir "$home/app" --port "$port")
  pid=$(field pid "$out")
  if [[ -n "$pid" ]]; then pids+=("$pid"); fi
  if ! grep -q '^Ergosphere is running' <<<"$out"; then
    bad "$name: install did not finish"
    printf '%s\n' "$out" | tail -n 15
    return
  fi
  saved=$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1])).get("port"))' "$cfg" 2>/dev/null)
  if [[ "$saved" == "$port" ]]; then ok "$name: port written to $cfg"; else bad "$name: $cfg holds port '${saved:-missing}', want $port"; fi
  if [[ "$(field config "$out")" == "$cfg" ]]; then ok "$name: summary reports $cfg"; else bad "$name: summary config is '$(field config "$out")', want $cfg"; fi
  if [[ "$cfg" != "$home/app/ergosphere.json" ]]; then
    if [[ -e "$home/app/ergosphere.json" ]]; then bad "$name: also wrote $home/app/ergosphere.json"; else ok "$name: no ergosphere.json next to ergosphere.py"; fi
    if [[ "$(field agents "$out")" == seeded=/tmp ]]; then ok "$name: agents read from $cfg"; else bad "$name: agents '$(field agents "$out")', want seeded=/tmp"; fi
  fi
  # A rerun with no --port takes the port from the same file and changes nothing.
  out=$(run_install "$home" --dir "$home/app")
  if [[ "$(field port "$out")" == "$port" && "$(field changed "$out")" == nothing* ]]; then
    ok "$name: rerun changed nothing"
  else
    bad "$name: rerun port '$(field port "$out")', changed '$(field changed "$out")'"
  fi
  kill "$pid" 2>/dev/null
  for _ in $(seq 50); do kill -0 "$pid" 2>/dev/null || break; sleep 0.1; done
}

unset ERGOSPHERE_CONFIG
check_case unset "$work/home1" "$work/home1/app/ergosphere.json"

# Seeded with the test port and an agent, so the server never falls back to
# its default port and the summary shows whether the installer read this file.
export ERGOSPHERE_CONFIG="$work/home2/state/ergosphere/settings.json"
mkdir -p "${ERGOSPHERE_CONFIG%/*}"
printf '{"port": %s, "agents": {"seeded": "/tmp"}}\n' "$port" >"$ERGOSPHERE_CONFIG"
check_case set "$work/home2" "$ERGOSPHERE_CONFIG"
unset ERGOSPHERE_CONFIG

exit "$fail"
