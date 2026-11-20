#!/usr/bin/env bash
# install.sh runs the downloaded release's own ergosphere.py --check, against
# the config this run would write, before it creates or changes anything:
# a release without vendor/xterm.js and a config the server rejects
# (last.effort) both stop at the check gate, in a real run and in --check.
#
# Run: ERGOSPHERE_TEST_PORT=<spare port> bash tests/install_verify_release_test.sh
# No network: the packages are served from 127.0.0.1 and grok is a stub that
# only answers --version. No case starts a server.
set -u
repo=$(cd "$(dirname "$0")/.." && pwd)
port=${ERGOSPHERE_TEST_PORT:-9570}
work=$(mktemp -d "${TMPDIR:-/tmp}/ergosphere-verify-test.XXXXXX")
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

# pack NAME [FILE TO DROP]: a release archive with one top folder.
pack() {
  local d="$work/pkg-$1/ergosphere-test"
  mkdir -p "$d"
  cp -p "$repo/ergosphere.py" "$repo/LICENSE" "$d/"
  cp -rp "$repo/vendor" "$d/"
  if [[ -n "${2:-}" ]]; then rm -f -- "$d/$2"; fi
  tar -czf "$work/www/$1.tar.gz" -C "$work/pkg-$1" ergosphere-test
}
mkdir -p "$work/bin" "$work/www"
pack good
pack novendor vendor/xterm.js
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
www="http://127.0.0.1:$(cat "$work/www.port")"

# inst HOME PKG CONFIG [installer args...]: prints the output, returns its rc.
inst() {
  local home=$1 pkg=$2 cfg=$3
  shift 3
  mkdir -p "$home"
  env -u ERGOSPHERE_PORT HOME="$home" PATH="$work/bin:$PATH" TMPDIR="$work" ERGOSPHERE_CONFIG="$cfg" \
    ERGOSPHERE_INSTALL_SOURCE_URL="$www/$pkg.tar.gz" \
    bash "$repo/install.sh" --dir "$home/app" --port "$port" "$@" </dev/null 2>&1
}

# expect_blocked NAME HOME CONFIG WANT RC OUT: refused at the gate, nothing made.
expect_blocked() {
  local name=$1 home=$2 cfg=$3 want=$4 rc=$5 out=$6
  if [[ "$rc" -ne 0 && "$out" == *"nothing was changed"* && "$out" == *"$want"* ]]; then
    ok "$name: blocked at the check gate"
  else
    bad "$name: rc $rc, want a gate refusal naming '$want'"
    printf '%s\n' "$out" | tail -n 8
  fi
  if [[ -e "$home/app" ]]; then bad "$name: $home/app was created"; else ok "$name: no install dir created"; fi
  if grep -qE '^(install|config): ' <<<"$out"; then bad "$name: files or config were written"; fi
}

for mode in --auto --check; do
  h="$work/v$mode"
  out=$(inst "$h" novendor "$h/state/ergosphere.json" "$mode"); rc=$?
  expect_blocked "missing vendor/xterm.js $mode" "$h" "$h/state/ergosphere.json" "missing vendor file: xterm.js" "$rc" "$out"
  if [[ -e "$h/state" ]]; then bad "missing vendor/xterm.js $mode: config folder was created"; else ok "missing vendor/xterm.js $mode: no config written"; fi

  h="$work/e$mode"
  mkdir -p "$h/state"
  printf '{"port": %s, "agents": {}, "last": {"model": "", "effort": "not valid!"}}\n' "$port" >"$h/state/ergosphere.json"
  before=$(sha256sum <"$h/state/ergosphere.json")
  out=$(inst "$h" good "$h/state/ergosphere.json" "$mode"); rc=$?
  expect_blocked "last.effort not valid $mode" "$h" "$h/state/ergosphere.json" "last.effort is not an effort level" "$rc" "$out"
  if [[ "$(sha256sum <"$h/state/ergosphere.json")" == "$before" && "$(ls -A "$h/state")" == ergosphere.json ]]; then
    ok "last.effort not valid $mode: config unchanged"
  else
    bad "last.effort not valid $mode: config folder changed"
  fi
done

# A good release still passes the check.
h="$work/good"
out=$(inst "$h" good "$h/state/ergosphere.json" --check); rc=$?
if [[ "$rc" -eq 0 && "$out" == *"ready: checks passed"* && ! -e "$h/app" && ! -e "$h/state" ]]; then
  ok "good release: --check passes and writes nothing"
else
  bad "good release --check: rc $rc"
  printf '%s\n' "$out" | tail -n 8
fi

exit "$fail"
