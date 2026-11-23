#!/bin/bash
# A/B section for issue #97: what install.sh copies into the install dir.
# Each side's install.sh installs the same package, the B tree packed as a
# release archive (developer files included), into a throwaway HOME on that
# side's port, reruns, then stops the server.  Prints the section for
# summary.txt; exit 1 when any check fails on B.
# Needs AB_A_DIR, AB_B_DIR, AB_PORT_A, AB_PORT_B, AB_OUT (see run.sh).
set -u
w="$AB_OUT/install"
rm -rf "$w"; mkdir -p "$w/www" "$w/pkg" "$w/bin" || exit 2
log() { printf '%s install_files.sh: %s\n' "$(date +%T)" "$*" >&2; }

# A tar of the B tree, not git archive: the A/B trees need not be git checkouts.
tar -c -C "$AB_B_DIR" --exclude=.git --exclude=__pycache__ --transform 's|^\.|ergosphere-ab|' . |
  tar -x -C "$w/pkg" || exit 2
tar -czf "$w/www/src.tar.gz" -C "$w/pkg" ergosphere-ab || exit 2
printf '#!/bin/sh\necho "grok 0.0.0-test"\n' >"$w/bin/grok"; chmod +x "$w/bin/grok"
python3 -I -c '
import functools, http.server, sys
h = functools.partial(http.server.SimpleHTTPRequestHandler, directory=sys.argv[1])
h.log_message = lambda *a: None
s = http.server.HTTPServer(("127.0.0.1", 0), h)
open(sys.argv[2], "w").write(str(s.server_address[1]))
s.serve_forever()' "$w/www" "$w/www.port" </dev/null >/dev/null 2>&1 &
www_pid=$!
trap 'kill "$www_pid" 2>/dev/null' EXIT
for _ in $(seq 100); do [ -s "$w/www.port" ] && break; sleep 0.1; done
url="http://127.0.0.1:$(cat "$w/www.port")/src.tar.gz"

field() { sed -n "s/^  $1: *//p" <<<"$2" | head -n 1; }
declare -A res
install_side() { # side code-dir port
  local side=$1 code=$2 port=$3 h="$w/$1" out pid files dev n want got
  mkdir -p "$h"
  run() {
    env -u ERGOSPHERE_PORT -u ERGOSPHERE_CONFIG HOME="$h" PATH="$w/bin:$PATH" TMPDIR="$w" \
      ERGOSPHERE_INSTALL_SOURCE_URL="$url" setsid -w bash "$code/install.sh" --auto --dir "$h/app" --port "$port" </dev/null 2>&1
  }
  log "$side: install with $code/install.sh on port $port"
  out=$(run); printf '%s\n' "$out" >"$h/install1.log"
  pid=$(field pid "$out")
  if ! grep -q '^Ergosphere is running' <<<"$out"; then
    res[$side.install]="FAIL did not finish"
    [ -n "$pid" ] && kill "$pid" 2>/dev/null
    return
  fi
  res[$side.install]="PASS finished"
  files=$(cd "$h/app" && find . -type f | sed 's|^\./||' | sort)
  printf '%s\n' "$files" >"$h/files.txt"
  dev=$(grep -cE '^(tests/|docs/|\.git|\.ghflow|install\.sh$)' <<<"$files")
  if [ "$dev" -eq 0 ]; then res[$side.dev_files]="PASS 0 of $(wc -l <<<"$files") files"
  else res[$side.dev_files]="FAIL $dev of $(wc -l <<<"$files") files"; fi
  n=0
  for f in ergosphere.py LICENSE vendor/xterm.js vendor/xterm.css vendor/addon-fit.js; do
    grep -qxF "$f" <<<"$files" && n=$((n + 1))
  done
  if [ "$n" -eq 5 ]; then res[$side.runtime]="PASS 5 of 5 present"; else res[$side.runtime]="FAIL $n of 5 present"; fi
  want=$(cd "$h/app" && python3 -I -c 'import hashlib as h; print(h.sha256(b"".join(h.sha256(open(f, "rb").read()).digest() for f in ("ergosphere.py", "vendor/addon-fit.js", "vendor/xterm.css", "vendor/xterm.js"))).hexdigest())')
  got=$(curl -fsS --max-time 5 "http://127.0.0.1:$port/health" |
    python3 -I -c 'import json,sys; print(json.load(sys.stdin)["code_sha256"])' 2>/dev/null)
  if [ -n "$got" ] && [ "$got" = "$want" ]; then res[$side.health]="PASS code_sha256 ${got:0:12}"
  else res[$side.health]="FAIL code_sha256 '${got:0:12}'"; fi
  out=$(run); printf '%s\n' "$out" >"$h/install2.log"
  if [[ "$(field changed "$out")" == nothing* ]]; then res[$side.rerun]="PASS changed nothing"
  else res[$side.rerun]="FAIL changed '$(field changed "$out")'"; fi
  kill "$pid" 2>/dev/null
  for _ in $(seq 50); do kill -0 "$pid" 2>/dev/null || break; sleep 0.1; done
  kill -9 "$pid" 2>/dev/null
}

install_side A "$AB_A_DIR" "$AB_PORT_A"
install_side B "$AB_B_DIR" "$AB_PORT_B"

echo
echo "Issue #97: files install.sh puts in the install dir (same package on both sides: the B tree with its developer files)"
printf '%-10s %-36s %-36s %s\n' check A B same
regress=() bfail=()
for c in install dev_files runtime health rerun; do
  a=${res[A.$c]:-"FAIL not run"} b=${res[B.$c]:-"FAIL not run"}
  same=NO; [ "$a" = "$b" ] && same=yes
  printf '%-10s %-36s %-36s %s\n' "$c" "${a:0:36}" "${b:0:36}" "$same"
  if [[ "$a" == PASS* && "$b" != PASS* ]]; then regress+=("$c"); fi
  if [[ "$b" != PASS* ]]; then bfail+=("$c"); fi
done
if [ ${#regress[@]} -gt 0 ]; then echo "REGRESSION: passes on A, fails on B: ${regress[*]}"; fi
if [ ${#bfail[@]} -gt 0 ]; then echo "INSTALL FAILURE on B: ${bfail[*]}"; exit 1; fi
exit 0
