#!/bin/bash
# A/B section for issue #194: install.sh checks the vendor files the release's
# own ergosphere.py names, not a list fixed in install.sh.  Packages built
# from the B tree: good (as is), renamed (vendor/xterm.js is xterm.min.js and
# its code says so), stale (the file renamed, its code still names xterm.js).
# Each side's install.sh runs --check on all three, then a real --auto install
# of renamed on that side's port; the server it starts is stopped.  Prints
# the section for summary.txt; exit 1 when any check fails on B.
# Needs AB_A_DIR, AB_B_DIR, AB_PORT_A, AB_PORT_B, AB_OUT (see run.sh).
set -u
w="$AB_OUT/vendor-rename"
rm -rf "$w"; mkdir -p "$w/www" "$w/bin" || exit 2
log() { printf '%s vendor_rename.sh: %s\n' "$(date +%T)" "$*" >&2; }

for p in good renamed stale; do
  mkdir -p "$w/pkg-$p/ergosphere-ab"
  cp -p "$AB_B_DIR/ergosphere.py" "$AB_B_DIR/LICENSE" "$w/pkg-$p/ergosphere-ab/" || exit 2
  cp -rp "$AB_B_DIR/vendor" "$w/pkg-$p/ergosphere-ab/" || exit 2
done
for p in renamed stale; do
  mv "$w/pkg-$p/ergosphere-ab/vendor/xterm.js" "$w/pkg-$p/ergosphere-ab/vendor/xterm.min.js" || exit 2
done
sed -i 's/xterm\.js"/xterm.min.js"/g' "$w/pkg-renamed/ergosphere-ab/ergosphere.py"
grep -q '("xterm.min.js", ' "$w/pkg-renamed/ergosphere-ab/ergosphere.py" || { log "renamed fixture not made"; exit 2; }
for p in good renamed stale; do tar -czf "$w/www/$p.tar.gz" -C "$w/pkg-$p" ergosphere-ab || exit 2; done
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
www="http://127.0.0.1:$(cat "$w/www.port")"

field() { sed -n "s/^  $1: *//p" <<<"$2" | head -n 1; }
declare -A res
# side code-dir port
check_side() {
  local side=$1 code=$2 port=$3 h="$w/$1" p mode out rc pid
  mkdir -p "$h"
  # run PKG MODE NAME: one installer run in its own HOME; output in NAME.log.
  run() {
    mkdir -p "$h/$3"
    env -u ERGOSPHERE_PORT -u ERGOSPHERE_CONFIG HOME="$h/$3" PATH="$w/bin:$PATH" TMPDIR="$w" \
      ERGOSPHERE_INSTALL_SOURCE_URL="$www/$1.tar.gz" \
      setsid -w bash "$code/install.sh" "$2" --dir "$h/$3/app" --port "$port" </dev/null >"$h/$3.log" 2>&1
  }
  for p in good renamed stale; do
    mode=--check
    log "$side: $code/install.sh $mode on $p"
    run "$p" "$mode" "$p-check"; rc=$?
    out=$(cat "$h/$p-check.log")
    case "$p:$rc" in
      good:0|renamed:0) res[$side.$p]="PASS --check passed" ;;
      stale:0) res[$side.$p]="FAIL --check passed" ;;
      stale:*)
        if [[ "$out" == *"archive is missing vendor/xterm.js"* && "$out" == *"nothing was changed"* ]]; then
          res[$side.$p]="PASS refused: missing xterm.js"
        else res[$side.$p]="FAIL rc $rc, other reason"; fi ;;
      *) res[$side.$p]="FAIL refused: $(grep -m1 -oE 'archive is missing [^ ]+' <<<"$out" || echo "rc $rc")" ;;
    esac
  done
  log "$side: $code/install.sh --auto on renamed, port $port"
  run renamed --auto renamed-auto; rc=$?
  out=$(cat "$h/renamed-auto.log")
  pid=$(field pid "$out")
  if [[ "$rc" -eq 0 ]] && curl -fsS --max-time 5 "http://127.0.0.1:$port/health" |
      python3 -I -c 'import json,sys; sys.exit(json.load(sys.stdin).get("product") != "ergosphere")' 2>/dev/null; then
    res[$side.install]="PASS installed, /health answers"
  else
    res[$side.install]="FAIL rc $rc: $(grep -m1 -oE 'archive is missing [^ ]+' <<<"$out" || echo 'no /health')"
  fi
  if [ -n "$pid" ]; then
    kill "$pid" 2>/dev/null
    for _ in $(seq 50); do kill -0 "$pid" 2>/dev/null || break; sleep 0.1; done
    kill -9 "$pid" 2>/dev/null
  fi
}

check_side A "$AB_A_DIR" "$AB_PORT_A"
check_side B "$AB_B_DIR" "$AB_PORT_B"

echo
echo "Issue #194: install.sh on a release that renamed vendor/xterm.js (packages from the B tree)"
printf '%-10s %-36s %-36s %s\n' check A B same
regress=() bfail=()
for c in good renamed stale install; do
  a=${res[A.$c]:-"FAIL not run"} b=${res[B.$c]:-"FAIL not run"}
  same=NO; [ "$a" = "$b" ] && same=yes
  printf '%-10s %-36s %-36s %s\n' "$c" "${a:0:36}" "${b:0:36}" "$same"
  if [[ "$a" == PASS* && "$b" != PASS* ]]; then regress+=("$c"); fi
  if [[ "$b" != PASS* ]]; then bfail+=("$c"); fi
done
if [ ${#regress[@]} -gt 0 ]; then echo "REGRESSION: passes on A, fails on B: ${regress[*]}"; fi
if [ ${#bfail[@]} -gt 0 ]; then echo "VENDOR CHECK FAILURE on B: ${bfail[*]}"; exit 1; fi
exit 0
