#!/usr/bin/env bash
# install.sh checks, before it changes anything, that every folder it will
# write in is writable and that each filesystem has room for what it copies.
# Cases, each in a real run and in --check: ERGOSPHERE_CONFIG in a read-only
# folder; an upgrade whose vendor/ is read-only (ergosphere.py must not be
# replaced); a disk without room (df reports 1 KiB free). Each stops at the
# check gate with nothing created or changed.
#
# Run: ERGOSPHERE_TEST_PORT=<spare port> bash tests/install_writable_test.sh
# No network: the package is served from 127.0.0.1 and grok is a stub that
# only answers --version. A server is started only when a case is not caught
# (main before the fix); it is stopped again.
set -u
repo=$(cd "$(dirname "$0")/.." && pwd)
port=${ERGOSPHERE_TEST_PORT:-9571}
work=$(mktemp -d "${TMPDIR:-/tmp}/ergosphere-writable-test.XXXXXX")
pids=()
fail=0

# shellcheck disable=SC2317  # run by the EXIT trap
cleanup() {
  local p
  for p in ${pids[@]+"${pids[@]}"}; do kill "$p" 2>/dev/null; done
  chmod -R u+w -- "$work" 2>/dev/null
  rm -rf -- "$work"
}
trap cleanup EXIT

ok() { echo "ok:   $1"; }
bad() { echo "FAIL: $1"; fail=1; }

d="$work/pkg/ergosphere-test"
mkdir -p "$d" "$work/bin" "$work/dfbin" "$work/www"
cp -p "$repo/ergosphere.py" "$repo/LICENSE" "$d/"
cp -rp "$repo/vendor" "$d/"
tar -czf "$work/www/good.tar.gz" -C "$work/pkg" ergosphere-test
printf '#!/bin/sh\necho "grok 0.0.0-test"\n' >"$work/bin/grok"
# A full disk without root: df reports 1 KiB free on every filesystem.
printf '#!/bin/sh\necho "Filesystem 1024-blocks Used Available Capacity Mounted on"\necho "/dev/full 100 99 1 99%% /"\n' >"$work/dfbin/df"
chmod +x "$work/bin/grok" "$work/dfbin/df"

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

# inst HOME CONFIG BINDIR [installer args...]: prints the output, returns its rc.
# setsid: no controlling terminal, so nothing can be answered on /dev/tty.
inst() {
  local home=$1 cfg=$2 bin=$3 out rc
  shift 3
  mkdir -p "$home"
  out=$(env -u ERGOSPHERE_PORT HOME="$home" PATH="$bin:$work/bin:$PATH" TMPDIR="$work" ERGOSPHERE_CONFIG="$cfg" \
    ERGOSPHERE_INSTALL_SOURCE_URL="$www/good.tar.gz" \
    setsid -w bash "$repo/install.sh" --dir "$home/app" --port "$port" "$@" </dev/null 2>&1); rc=$?
  # A run the gate did not stop may have started a server: stop it.
  # Wait until it is gone, so the next case finds the port free.
  # KEEP_SERVER=1 leaves it running for the next run (stop_server stops it).
  if [[ "$out" =~ start:\ started\ pid\ ([0-9]+) && "${KEEP_SERVER:-0}" == 1 ]]; then
    printf '%s\n' "${BASH_REMATCH[1]}" >"$work/kept.pid"
  elif [[ "$out" =~ start:\ started\ pid\ ([0-9]+) ]]; then
    kill "${BASH_REMATCH[1]}" 2>/dev/null
    for _ in $(seq 100); do kill -0 "${BASH_REMATCH[1]}" 2>/dev/null || break; sleep 0.1; done
  fi
  printf '%s\n' "$out"
  return "$rc"
}

# tree DIR: every path under DIR with its sha256, so "nothing changed" is checked by content.
tree() {
  (cd "$1" 2>/dev/null && find . | sort && find . -type f -exec sha256sum {} + | sort)
}

# expect_blocked NAME WANT RC OUT: refused at the gate, nothing written.
expect_blocked() {
  local name=$1 want=$2 rc=$3 out=$4
  if [[ "$rc" -ne 0 && "$out" == *"nothing was changed"* && "$out" == *"$want"* ]]; then
    ok "$name: blocked at the check gate"
  else
    bad "$name: rc $rc, want a gate refusal naming '$want'"
    printf '%s\n' "$out" | tail -n 8
  fi
  if grep -qE '^(install|config|start): ' <<<"$out"; then bad "$name: files, config or server were changed"; fi
}

for mode in --auto --check; do
  # ERGOSPHERE_CONFIG in a read-only folder: before the fix 7 files were
  # copied, then the config write failed.
  h="$work/rocfg$mode"
  mkdir -p "$h/ro"
  chmod 555 "$h/ro"
  out=$(inst "$h" "$h/ro/state/ergosphere.json" "$work/none" "$mode"); rc=$?
  expect_blocked "read-only config folder $mode" "not writable: $h/ro" "$rc" "$out"
  if [[ -e "$h/app" ]]; then bad "read-only config folder $mode: $h/app was created"; else ok "read-only config folder $mode: no install dir created"; fi

  # An upgrade with a read-only vendor/: before the fix ergosphere.py was
  # replaced first, then vendor/ failed, leaving new code with old vendor files.
  h="$work/rovendor$mode"
  mkdir -p "$h/app" "$h/state"
  cp -p "$repo/LICENSE" "$h/app/"
  cp -rp "$repo/vendor" "$h/app/"
  { cat "$repo/ergosphere.py"; echo "# older release"; } >"$h/app/ergosphere.py"
  echo "/* older release */" >>"$h/app/vendor/xterm.js"
  chmod 555 "$h/app/vendor"
  before=$(tree "$h")
  out=$(inst "$h" "$h/state/ergosphere.json" "$work/none" "$mode"); rc=$?
  expect_blocked "read-only vendor/ $mode" "not writable: $h/app/vendor" "$rc" "$out"
  if [[ "$(tree "$h")" == "$before" ]]; then ok "read-only vendor/ $mode: install dir and config unchanged"; else bad "read-only vendor/ $mode: files changed (ergosphere.py replaced?)"; fi

  # A disk without room for the release.
  h="$work/full$mode"
  out=$(inst "$h" "$h/state/ergosphere.json" "$work/dfbin" "$mode"); rc=$?
  expect_blocked "full disk $mode" "has 1 KiB free, the release needs" "$rc" "$out"
  if [[ -e "$h/app" || -e "$h/state" ]]; then bad "full disk $mode: install dir or config created"; else ok "full disk $mode: nothing created"; fi
done

# A read-only ergosphere.log is no blocker when the rerun reuses the running
# server, which never opens the log (review finding on PR 197).
h="$work/rolog"
out=$(KEEP_SERVER=1 inst "$h" "$h/state/ergosphere.json" "$work/none" --auto); rc=$?
kept=$(cat "$work/kept.pid" 2>/dev/null)
if [[ "$rc" -eq 0 && -n "$kept" ]]; then
  pids+=("$kept")
  chmod 444 "$h/app/ergosphere.log"
  out=$(inst "$h" "$h/state/ergosphere.json" "$work/none" --auto); rc=$?
  if [[ "$rc" -eq 0 && "$out" == *"start: already running, pid $kept"* ]]; then
    ok "read-only log, server running: rerun reuses pid $kept"
  else
    bad "read-only log, server running: rc $rc, want the running server reused"
    printf '%s\n' "$out" | tail -n 8
  fi
  kill "$kept" 2>/dev/null
  for _ in $(seq 100); do kill -0 "$kept" 2>/dev/null || break; sleep 0.1; done
else
  bad "read-only log: first install failed, rc $rc"
  printf '%s\n' "$out" | tail -n 8
fi

# An upgrade where every file changed in place (same size, new bytes). Each copy
# is moved over its old file, which frees that file's space, so the disk needs
# room for the largest file, not for the sum. df reports exactly the largest
# file: the gate must let it through, since the sequential replace fits.
h="$work/seqfit"
mkdir -p "$h/app" "$h/state" "$work/dfpeak"
while IFS= read -r f; do
  mkdir -p "$h/app/$(dirname -- "$f")"
  cp -p "$d/$f" "$h/app/$f"
  printf 'X' | dd of="$h/app/$f" bs=1 seek=0 conv=notrunc status=none
done < <(cd "$d" && find . -type f | sed 's|^\./||')
blocks() { echo $((4 * (($(stat -c %s -- "$1") + 4095) / 4096))); }
total=0 max=0
while IFS= read -r f; do
  b=$(blocks "$d/$f"); total=$((total + b)); (( b > max )) && max=$b
done < <(cd "$d" && find . -type f | sed 's|^\./||')
if (( total <= max )); then bad "sequential fit: fixture needs no more than its largest file"; fi
printf '#!/bin/sh\necho "Filesystem 1024-blocks Used Available Capacity Mounted on"\necho "/dev/seq 100 99 %d 99%% /"\n' "$max" >"$work/dfpeak/df"
chmod +x "$work/dfpeak/df"
out=$(inst "$h" "$h/state/ergosphere.json" "$work/dfpeak" --check); rc=$?
if [[ "$rc" -eq 0 && "$out" == *"ready: checks passed"* ]]; then
  ok "upgrade in place with room for the largest file (sum $total KiB, free $max KiB): --check passes"
else
  bad "upgrade in place: rc $rc, want --check to pass with $max KiB free (sum $total KiB)"
  printf '%s\n' "$out" | tail -n 8
fi
# One block less than the largest file: the sequential replace cannot fit.
printf '#!/bin/sh\necho "Filesystem 1024-blocks Used Available Capacity Mounted on"\necho "/dev/seq 100 99 %d 99%% /"\n' "$((max - 4))" >"$work/dfpeak/df"
out=$(inst "$h" "$h/state/ergosphere.json" "$work/dfpeak" --check); rc=$?
expect_blocked "upgrade in place, one block short" "has $((max - 4)) KiB free, the release needs $max KiB" "$rc" "$out"

# A writable config folder whose name ends in a newline (no folder of the name
# without it) passes: the nearest-parent walk keeps the name intact.
h="$work/nlcfg"
mkdir -p "$h/cfg"$'\n'
out=$(inst "$h" "$h/cfg"$'\n'"/ergosphere.json" "$work/none" --check); rc=$?
if [[ "$rc" -eq 0 && "$out" == *"ready: checks passed"* ]]; then
  ok "config folder name ending in a newline: --check passes"
else
  bad "config folder name ending in a newline: rc $rc, want --check to pass"
  printf '%s\n' "$out" | tail -n 8
fi

exit "$fail"
