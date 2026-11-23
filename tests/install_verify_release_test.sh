#!/usr/bin/env bash
# install.sh checks the downloaded release before it creates or changes
# anything. A release without vendor/xterm.js stops at the check gate, in a
# real run and in --check. A real run also runs the release's own
# ergosphere.py --check against the config it would write, so a config the
# server rejects (last.effort) stops there too. --check runs nothing from the
# download: a planted ergosphere.py that writes a marker never runs under it.
# The vendor files checked are the ones the release's own ergosphere.py names
# in VENDOR_FILES, read without running it (#194): a release that renames a
# file and its code passes; one whose code names a file it lacks is refused.
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
  # A case that wrongly passes the gate in --auto starts a server from its
  # install dir under $work; stop it so it does not keep the port.
  pkill -f -- "$work/[^ ]*/app/ergosphere.py" 2>/dev/null
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
mark="$work/planted-ran"
pack planted
# The planted lines run first; the rest is the real file, so its vendor list
# still reads.
{ printf 'open(%s, "w").close()\nraise SystemExit("planted release ran")\n' "'$mark'"
  cat "$repo/ergosphere.py"; } >"$work/pkg-planted/ergosphere-test/ergosphere.py"
tar -czf "$work/www/planted.tar.gz" -C "$work/pkg-planted" ergosphere-test
# renamed: vendor/xterm.js is now vendor/xterm.min.js, and its code says so.
# stale: the same file renamed, but its code still names vendor/xterm.js.
# novlist: VENDOR_FILES is no plain literal (it still works when run).
for p in renamed stale novlist; do pack "$p"; done
for p in renamed stale; do
  mv "$work/pkg-$p/ergosphere-test/vendor/xterm.js" "$work/pkg-$p/ergosphere-test/vendor/xterm.min.js"
done
sed -i 's/xterm\.js"/xterm.min.js"/g' "$work/pkg-renamed/ergosphere-test/ergosphere.py"
sed -i 's/("xterm\.js", /("xterm" + ".js", /' "$work/pkg-novlist/ergosphere-test/ergosphere.py"
if ! grep -q '("xterm.min.js", ' "$work/pkg-renamed/ergosphere-test/ergosphere.py" ||
    ! grep -q '("xterm" + ".js", ' "$work/pkg-novlist/ergosphere-test/ergosphere.py"; then
  echo "FAIL: renamed/novlist fixtures not made"; exit 1
fi
for p in renamed stale novlist; do tar -czf "$work/www/$p.tar.gz" -C "$work/pkg-$p" ergosphere-test; done
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
# setsid: no controlling terminal, so nothing can be answered on /dev/tty.
inst() {
  local home=$1 pkg=$2 cfg=$3
  shift 3
  mkdir -p "$home"
  env -u ERGOSPHERE_PORT HOME="$home" PATH="$work/bin:$PATH" TMPDIR="$work" ERGOSPHERE_CONFIG="$cfg" \
    ERGOSPHERE_INSTALL_SOURCE_URL="$www/$pkg.tar.gz" \
    setsid -w bash "$repo/install.sh" --dir "$home/app" --port "$port" "$@" </dev/null 2>&1
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

bad_effort() {
  mkdir -p "$1/state"
  printf '{"port": %s, "agents": {}, "last": {"model": "", "effort": "not valid!"}}\n' "$port" >"$1/state/ergosphere.json"
}

for mode in --auto --check; do
  h="$work/v$mode"
  out=$(inst "$h" novendor "$h/state/ergosphere.json" "$mode"); rc=$?
  expect_blocked "missing vendor/xterm.js $mode" "$h" "$h/state/ergosphere.json" "archive is missing vendor/xterm.js" "$rc" "$out"
  if [[ -e "$h/state" ]]; then bad "missing vendor/xterm.js $mode: config folder was created"; else ok "missing vendor/xterm.js $mode: no config written"; fi
done

# A renamed vendor file passes --check when the release's own code names it.
# install_e2e_test.sh installs that release for real.
h="$work/renamed"
out=$(inst "$h" renamed "$h/state/ergosphere.json" --check); rc=$?
if [[ "$rc" -eq 0 && "$out" == *"ready: checks passed"* && ! -e "$h/app" && ! -e "$h/state" ]]; then
  ok "renamed vendor file its code names: --check passes"
else
  bad "renamed vendor file its code names --check: rc $rc"
  printf '%s\n' "$out" | tail -n 8
fi
for mode in --auto --check; do
  h="$work/stale$mode"
  out=$(inst "$h" stale "$h/state/ergosphere.json" "$mode"); rc=$?
  expect_blocked "renamed file, code names the old one $mode" "$h" "$h/state/ergosphere.json" "archive is missing vendor/xterm.js" "$rc" "$out"
  h="$work/novlist$mode"
  out=$(inst "$h" novlist "$h/state/ergosphere.json" "$mode"); rc=$?
  expect_blocked "VENDOR_FILES not a literal $mode" "$h" "$h/state/ergosphere.json" "cannot read the vendor file list" "$rc" "$out"
done

# The real run catches a config the server rejects before any copy.
h="$work/effort"
bad_effort "$h"
before=$(sha256sum <"$h/state/ergosphere.json")
out=$(inst "$h" good "$h/state/ergosphere.json" --auto); rc=$?
expect_blocked "last.effort not valid" "$h" "$h/state/ergosphere.json" "$h/state/ergosphere.json last.effort is not an effort level" "$rc" "$out"
if [[ "$(sha256sum <"$h/state/ergosphere.json")" == "$before" && "$(ls -A "$h/state")" == ergosphere.json ]]; then
  ok "last.effort not valid: config unchanged"
else
  bad "last.effort not valid: config folder changed"
fi

# --check runs nothing from the download; the real run does run its --check.
h="$work/plant-check"
out=$(inst "$h" planted "$h/state/ergosphere.json" --check); rc=$?
if [[ "$rc" -eq 0 && "$out" == *"ready: checks passed"* && ! -e "$mark" && ! -e "$h/app" ]]; then
  ok "planted release: --check ran nothing from it"
else
  bad "planted release --check: rc $rc, marker $([[ -e "$mark" ]] && echo written || echo absent)"
  printf '%s\n' "$out" | tail -n 8
fi
h="$work/plant-auto"
out=$(inst "$h" planted "$h/state/ergosphere.json" --auto); rc=$?
if [[ -e "$mark" ]]; then ok "planted release: the real run runs its --check"; else bad "planted release: the real run did not run its --check"; fi
expect_blocked "planted release --auto" "$h" "$h/state/ergosphere.json" "planted release ran" "$rc" "$out"
rm -f -- "$mark"
# A non-loopback bind with no --yes and no terminal stops at the approval,
# before the real run executes anything from the download.
h="$work/plant-bind"
out=$(inst "$h" planted "$h/state/ergosphere.json" --auto --bind 0.0.0.0); rc=$?
if [[ "$rc" -ne 0 && ! -e "$mark" && ! -e "$h/app" && "$out" == *"needs confirmation on a terminal, or --yes"* ]]; then
  ok "planted release, unapproved bind: stopped at the approval, nothing ran"
else
  bad "planted release, unapproved bind: rc $rc, marker $([[ -e "$mark" ]] && echo written || echo absent)"
  printf '%s\n' "$out" | tail -n 8
fi
rm -f -- "$mark"

# A good release still passes --check.
h="$work/good"
out=$(inst "$h" good "$h/state/ergosphere.json" --check); rc=$?
if [[ "$rc" -eq 0 && "$out" == *"ready: checks passed"* && ! -e "$h/app" && ! -e "$h/state" ]]; then
  ok "good release: --check passes and writes nothing"
else
  bad "good release --check: rc $rc"
  printf '%s\n' "$out" | tail -n 8
fi

# An unreadable legacy cwd-defaults.json is ignored when ergosphere.json exists
# (the server and cfg_py both prefer the primary file): the release check
# still runs and reports the primary file's problem.
h="$work/unreadable-legacy"
bad_effort "$h"
printf '{"x": "/tmp"}\n' >"$h/state/cwd-defaults.json"
chmod 000 "$h/state/cwd-defaults.json"
out=$(inst "$h" good "$h/state/ergosphere.json" --auto); rc=$?
expect_blocked "unreadable legacy file" "$h" "$h/state/ergosphere.json" "last.effort is not an effort level" "$rc" "$out"
chmod 644 "$h/state/cwd-defaults.json"

# A relative TMPDIR makes the staging dir relative. The release check runs
# after install.sh cds into it, so the staging paths must still resolve.
h="$work/reltmp"
bad_effort "$h"
mkdir -p "$work/cwd/tmp"
out=$(cd "$work/cwd" && env -u ERGOSPHERE_PORT HOME="$h" PATH="$work/bin:$PATH" TMPDIR=tmp \
  ERGOSPHERE_CONFIG="$h/state/ergosphere.json" ERGOSPHERE_INSTALL_SOURCE_URL="$www/good.tar.gz" \
  bash "$repo/install.sh" --dir "$h/app" --port "$port" --auto </dev/null 2>&1); rc=$?
expect_blocked "relative TMPDIR" "$h" "$h/state/ergosphere.json" "last.effort is not an effort level" "$rc" "$out"

exit "$fail"
