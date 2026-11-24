#!/bin/bash
# A/B run for ghflow (ghflow ship/check --ab 'bash "$AB_B_DIR/tests/ab/run.sh"').
# Contract: AB_A_DIR (base) and AB_B_DIR (PR plus base) checkouts, AB_PORT_A and
# AB_PORT_B free ports, AB_OUT a writable folder.  Each side runs its own
# ergosphere.py with a throwaway HOME, one seeded session and the stand-in grok
# TUI (fakegrok.py), then the shared checks from this folder.  Writes
# $AB_OUT/summary.txt.  Exit 1 when a check passes on A and fails on B, when the
# browser checks break on B or leave out a check named in ab.py ORDER, or the
# run itself breaks (a server never reports ready, or no check passes on A).
# No browser (or node, playwright) for B fails too, unless AB_NO_BROWSER=1.
# Optional: AB_CHROME (browser binary), NODE_PATH (playwright), AB_NO_BROWSER.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# A fresh id per run: Ergosphere finds a session's holder machine-wide via
# /proc, so a fixed id makes concurrent runs on one machine block each other.
SID="$(cat /proc/sys/kernel/random/uuid)" || exit 2
for v in AB_A_DIR AB_B_DIR AB_PORT_A AB_PORT_B AB_OUT; do
  [ -n "${!v:-}" ] || { echo "run.sh: $v is not set" >&2; exit 2; }
done
mkdir -p "$AB_OUT" || exit 2

log() { printf '%s run.sh: %s\n' "$(date +%T)" "$*" >&2; }

# Playwright and a browser: found here, not installed (the sandbox has no network).
pw_path="${NODE_PATH:-}"
for d in "$(npm root -g 2>/dev/null)" /usr/local/lib/*/node_modules; do
  [ -d "$d/playwright" ] && pw_path="${pw_path:+$pw_path:}$d"
done
if [ -z "${AB_CHROME:-}" ]; then
  for c in google-chrome chromium chromium-browser; do
    AB_CHROME="$(command -v "$c" 2>/dev/null)" && break
  done
fi
export AB_CHROME="${AB_CHROME:-}"
browser_off=""
if ! command -v node >/dev/null 2>&1; then browser_off="node not found"
elif ! NODE_PATH="$pw_path" node -e 'require.resolve("playwright")' >/dev/null 2>&1; then
  browser_off="playwright module not found (set NODE_PATH)"
fi
log "playwright path: ${pw_path:-none}; browser: ${AB_CHROME:-playwright default}"
# Page check modules (#243): node preloads each browser.d/*.js, which adds its
# check to browser_d.js; browser.js runs them before resume_box.
page_mods=()
# Names a module may not reuse, from ab.py (the one list).
AB_BUILTIN_CHECKS="$(python3 -B "$HERE/ab.py" builtin)" || exit 2
export AB_BUILTIN_CHECKS
for f in "$HERE"/browser.d/*.js; do [ -f "$f" ] && page_mods+=(-r "$f"); done

run_side() { # label code-dir port
  local label="$1" code="$2" port="$3" w="$AB_OUT/$1" pid brc
  rm -rf "$w"; mkdir -p "$w/home/.grok/sessions/ab/$SID" "$w/work" "$w/bin"
  printf '{"info":{"id":"%s","cwd":"%s"},"generated_title":"AB seeded session","updated_at":"2026-01-01T00:00:00Z"}\n' \
    "$SID" "$w/work" >"$w/home/.grok/sessions/ab/$SID/summary.json"
  printf '{"bind":"127.0.0.1","port":%s}\n' "$port" >"$w/config.json"
  # argv[0] must be "grok": Ergosphere checks /proc/<pid>/cmdline.
  printf '#!/bin/bash\nexec -a grok python3 -B "%s/fakegrok.py" "$@"\n' "$HERE" >"$w/bin/grok"
  chmod +x "$w/bin/grok"
  log "$label: start $code/ergosphere.py on 127.0.0.1:$port"
  (cd "$w" && HOME="$w/home" ERGOSPHERE_CONFIG="$w/config.json" PATH="$w/bin:$PATH" \
     exec python3 -B "$code/ergosphere.py" --bind 127.0.0.1 --port "$port") >"$w/server.log" 2>&1 &
  pid=$!
  # Wait for the ready line, not a time: a loaded machine can take long to start.
  for _ in $(seq 600); do
    grep -q ERGOSPHERE_READY "$w/server.log" && break
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.1
  done
  grep -q ERGOSPHERE_READY "$w/server.log" || log "$label: server not ready: $(tail -n 3 "$w/server.log")"
  python3 -B "$HERE/ab.py" http "$port" "$SID" >"$w/http.json" 2>"$w/http.err"
  python3 -B "$HERE/ab.py" vendor "$code" >"$w/vendor.json" 2>"$w/vendor.err"
  if [ -z "$browser_off" ]; then
    brc=0
    NODE_PATH="$pw_path" timeout 300 node ${page_mods[@]+"${page_mods[@]}"} "$HERE/browser.js" "$port" "$SID" >"$w/browser.json" 2>"$w/browser.err" || brc=$?
    [ -s "$w/browser.json" ] || printf '{"error":"browser.js exited %s with no result"}\n' "$brc" >"$w/browser.json"
  else
    printf '{"unavailable":"%s"}\n' "$browser_off" >"$w/browser.json"
  fi
  kill "$pid" 2>/dev/null
  for _ in $(seq 50); do kill -0 "$pid" 2>/dev/null || break; sleep 0.1; done
  kill -9 "$pid" 2>/dev/null
  wait "$pid" 2>/dev/null
  log "$label: http $(cat "$w/http.json")"
  log "$label: browser $(cat "$w/browser.json")"
}

run_side A "$AB_A_DIR" "$AB_PORT_A"
run_side B "$AB_B_DIR" "$AB_PORT_B"
rc=0
python3 -B "$HERE/ab.py" summary "$AB_OUT/A" "$AB_OUT/B" >"$AB_OUT/summary.txt" || rc=$?
# Issue #113: a side that never started, or an A that passed nothing, means
# the run tested nothing; equal failures on both sides are not a pass.
for side in A B; do
  grep -q ERGOSPHERE_READY "$AB_OUT/$side/server.log" && continue
  echo "RUN BROKE: $side server never reported ready (see $side/server.log)" >>"$AB_OUT/summary.txt"
  rc=1
done
if ! python3 -B -c 'import sys; sys.path.insert(0, sys.argv[1]); import ab
sys.exit(0 if any(c.get("ok") for c in ab.side(sys.argv[2])[0].values()) else 1)' "$HERE" "$AB_OUT/A"; then
  echo "RUN BROKE: no check passed on A" >>"$AB_OUT/summary.txt"
  rc=1
fi
# Issue #129: no browser on B means no page check ran there, so a page
# regression would pass.  ghflow's A/B host has one; AB_NO_BROWSER=1 allows a
# local run without it (ghflow clears the environment, so it must be in --ab).
b_why="$(python3 -B -c 'import json, sys
d = json.load(open(sys.argv[1]))
print("" if d.get("browser") else str(d.get("unavailable") or d.get("error") or "no result")[:200])' "$AB_OUT/B/browser.json" 2>/dev/null)" || b_why="no readable B/browser.json"
if [ -n "$b_why" ]; then
  if [ "${AB_NO_BROWSER:-}" = 1 ]; then
    echo "WARNING: browser checks did not run on B ($b_why); allowed by AB_NO_BROWSER=1" >>"$AB_OUT/summary.txt"
  else
    echo "RUN BROKE: browser checks did not run on B ($b_why); set AB_NO_BROWSER=1 to allow that" >>"$AB_OUT/summary.txt"
    rc=1
  fi
fi
# Issue #64: a running server keeps the vendor files it started with.
echo $'\nIssue #64: vendor files replaced on disk under a running server, then a Settings restart' >>"$AB_OUT/summary.txt"
for side in A B; do
  dir=AB_${side}_DIR port=AB_PORT_${side}
  out="$(ERGOSPHERE_TEST_PORT="${!port}" python3 -B "$HERE/../vendor_snapshot_test.py" "${!dir}" 2>&1)" || { [ "$side" = B ] && rc=1; }
  printf '%s: %s\n' "$side" "$(printf '%s\n' "$out" | grep -E '^(PASS|FAIL)' | grep -v 'server output' | tr '\n' ' ')" >>"$AB_OUT/summary.txt"
done
# Issue #134: a page held by another server (the other side's port runs the holder server).
for side in A B; do
  dir=AB_${side}_DIR port=AB_PORT_${side} other=AB_PORT_B
  [ "$side" = B ] && other=AB_PORT_A
  rm -rf "$AB_OUT/held-$side"; mkdir -p "$AB_OUT/held-$side"
  log "$side: held page check on ${!port} (holder ${!other})"
  NODE_PATH="$pw_path" python3 -B "$HERE/held_quit.py" run "${!dir}" "${!port}" "${!other}" "$AB_OUT/held-$side" \
    >"$AB_OUT/held-$side.json" 2>"$AB_OUT/held-$side.err"
done
python3 -B "$HERE/held_quit.py" summary "$AB_OUT/held-A.json" "$AB_OUT/held-B.json" >>"$AB_OUT/summary.txt" || rc=1
# Issue #70: log writes blocked (full stdout pipe), on each side's port again.
for side in A B; do
  dir=AB_${side}_DIR port=AB_PORT_${side}
  rm -rf "$AB_OUT/log-$side"; mkdir -p "$AB_OUT/log-$side"
  log "$side: blocked log check on ${!port}"
  python3 -B "$HERE/blocked_log.py" run "${!dir}" "${!port}" "$AB_OUT/log-$side" >"$AB_OUT/log-$side.json" 2>"$AB_OUT/log-$side.err"
done
python3 -B "$HERE/blocked_log.py" summary "$AB_OUT/log-A.json" "$AB_OUT/log-B.json" >>"$AB_OUT/summary.txt" || rc=1
# Issue #97: what each side's install.sh copies into the install dir.
bash "$HERE/install_files.sh" >>"$AB_OUT/summary.txt" || rc=1
# Issue #140: --check stops where the real run stops and says what it does with the server.
bash "$HERE/install_check_plan.sh" >>"$AB_OUT/summary.txt" || rc=1
# Issue #63: sessions between the installer's session check and its restart.
bash "$HERE/install_restart_gate.sh" >>"$AB_OUT/summary.txt" || rc=1
# Issue #194: a release that renamed a vendor file, checked by each side's install.sh.
bash "$HERE/vendor_rename.sh" >>"$AB_OUT/summary.txt" || rc=1
# Issue #243: every checks/*.sh, after the sections above.  A new A/B check is a
# new file in checks/ (or browser.d/ for a page check); never add a block here.
export AB_A_DIR AB_B_DIR AB_PORT_A AB_PORT_B AB_OUT
NODE_PATH="$pw_path" bash "$HERE/run_checks.sh" "$HERE/checks" "$AB_A_DIR/tests/ab/checks" >>"$AB_OUT/summary.txt" || rc=1
cat "$AB_OUT/summary.txt"
[ -s "$AB_OUT/summary.txt" ] || exit 1
exit "$rc"
