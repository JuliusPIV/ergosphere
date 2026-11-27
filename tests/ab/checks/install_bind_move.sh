#!/bin/bash
# A/B section for issue #142: a rerun that changes only the bind (same port)
# while another program already listens on the new address.  Each side's
# install.sh, on that side's port, with a throwaway HOME and the B tree's
# ergosphere.py as the package:
# 1. own server on 127.0.0.2:P, another program on 127.0.0.1:P, rerun
#    --bind 127.0.0.1 (must fail at check, config and both servers kept);
# 2. own server on 127.0.0.1:P, another program on 127.0.0.2:P, rerun
#    --bind 0.0.0.0 (same);
# 3. nothing else on the port: own server on 127.0.0.2:P moves to 127.0.0.1:P.
# Prints the section for summary.txt; exit 1 when any check fails on B.
# Needs AB_A_DIR, AB_B_DIR, AB_PORT_A, AB_PORT_B, AB_OUT (see run.sh).
set -u
w="$AB_OUT/bind-move"
rm -rf "$w"; mkdir -p "$w/www" "$w/pkg/ergosphere-ab" "$w/bin" || exit 2
log() { printf '%s install_bind_move.sh: %s\n' "$(date +%T)" "$*" >&2; }
pids=()
# shellcheck disable=SC2317  # run by the EXIT trap
cleanup() { local p; for p in ${pids[@]+"${pids[@]}"}; do kill "$p" 2>/dev/null; done; }
trap cleanup EXIT

cp -p "$AB_B_DIR/ergosphere.py" "$AB_B_DIR/LICENSE" "$w/pkg/ergosphere-ab/" || exit 2
cp -rp "$AB_B_DIR/vendor" "$w/pkg/ergosphere-ab/" || exit 2
tar -czf "$w/www/src.tar.gz" -C "$w/pkg" ergosphere-ab || exit 2
printf '#!/bin/sh\necho "grok 0.0.0-test"\n' >"$w/bin/grok"; chmod +x "$w/bin/grok"
python3 -I -c '
import functools, http.server, sys
h = functools.partial(http.server.SimpleHTTPRequestHandler, directory=sys.argv[1])
h.log_message = lambda *a: None
s = http.server.HTTPServer(("127.0.0.1", 0), h)
open(sys.argv[2], "w").write(str(s.server_address[1]))
s.serve_forever()' "$w/www" "$w/www.port" </dev/null >/dev/null 2>&1 &
pids+=($!)
for _ in $(seq 100); do [ -s "$w/www.port" ] && break; sleep 0.1; done
url="http://127.0.0.1:$(cat "$w/www.port")/src.tar.gz"

field() { sed -n "s/^  $1: *//p" <<<"$2" | head -n 1; }
health_pid() {
  curl --noproxy '*' -s --max-time 3 "http://$1/health" |
    python3 -I -c 'import json,sys; d=json.load(sys.stdin); print(d["pid"] if d.get("product") == "ergosphere" else "")' 2>/dev/null
}
stop() { local p; for p in "$@"; do [ -n "$p" ] && kill "$p" 2>/dev/null; done
  for p in "$@"; do [ -n "$p" ] || continue; for _ in $(seq 50); do kill -0 "$p" 2>/dev/null || break; sleep 0.1; done; done; }
declare -A res

# held side code port tag own-bind other-bind new-bind: one taken-address case.
held() {
  local side=$1 code=$2 port=$3 tag=$4 h="$w/$1/$4" out rc other old before
  mkdir -p "$h"
  run() {
    env -u ERGOSPHERE_PORT -u ERGOSPHERE_CONFIG HOME="$h" PATH="$w/bin:$PATH" TMPDIR="$w" \
      ERGOSPHERE_INSTALL_SOURCE_URL="$url" bash "$code/install.sh" --auto --yes --dir "$h/app" "$@" </dev/null 2>&1
  }
  out=$(run --bind "$5" --port "$port"); printf '%s\n' "$out" >"$h/install-own.log"
  old=$(field pid "$out"); [ -n "$old" ] && pids+=("$old")
  if [ -z "$old" ] || [ "$(health_pid "$5:$port")" != "$old" ]; then
    res[$side.${tag}_check]="FAIL own server did not start"; stop "$old"; return
  fi
  python3 -I -c '
import http.server, sys
class Quiet(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self): self.send_error(404)
s = http.server.HTTPServer((sys.argv[1], int(sys.argv[2])), Quiet)
open(sys.argv[3], "w").write("up")
s.serve_forever()' "$6" "$port" "$h/other.up" </dev/null >/dev/null 2>&1 &
  other=$!; pids+=("$other")
  for _ in $(seq 100); do [ -s "$h/other.up" ] && break; sleep 0.1; done
  if [ ! -s "$h/other.up" ]; then res[$side.${tag}_check]="FAIL no listener on $6"; stop "$old" "$other"; return; fi
  before=$(cat "$h/app/ergosphere.json")
  log "$side: own on $5:$port, other on $6:$port; rerun --bind $7"
  rc=0; out=$(run --bind "$7" --port "$port") || rc=$?
  printf '%s\n' "$out" >"$h/install-move.log"
  res[$side.${tag}_check]="FAIL rc $rc, $(grep -o '^failed at [a-z]*' <<<"$out" || echo 'no failure')"
  if [ "$rc" -ne 0 ] && grep -q '^failed at check' <<<"$out"; then res[$side.${tag}_check]="PASS failed at check"; fi
  if [ "$(cat "$h/app/ergosphere.json")" = "$before" ]; then res[$side.${tag}_config]="PASS unchanged"
  else res[$side.${tag}_config]="FAIL rewritten (bind $7)"; fi
  if [ "$(health_pid "$5:$port")" = "$old" ]; then res[$side.${tag}_kept]="PASS old pid still serves"
  else res[$side.${tag}_kept]="FAIL old server gone"; fi
  stop "$old" "$other" "$(field pid "$out")"
}

# free side code port: bind-only move with the new address free.
free() {
  local side=$1 code=$2 port=$3 h="$w/$1/free" out rc old new
  mkdir -p "$h"
  run() {
    env -u ERGOSPHERE_PORT -u ERGOSPHERE_CONFIG HOME="$h" PATH="$w/bin:$PATH" TMPDIR="$w" \
      ERGOSPHERE_INSTALL_SOURCE_URL="$url" bash "$code/install.sh" --auto --yes --dir "$h/app" "$@" </dev/null 2>&1
  }
  log "$side: own on 127.0.0.2:$port, rerun --bind 127.0.0.1"
  out=$(run --bind 127.0.0.2 --port "$port"); printf '%s\n' "$out" >"$h/install-own.log"
  old=$(field pid "$out"); [ -n "$old" ] && pids+=("$old")
  rc=0; out=$(run --bind 127.0.0.1 --port "$port") || rc=$?
  printf '%s\n' "$out" >"$h/install-move.log"
  new=$(field pid "$out"); [ -n "$new" ] && pids+=("$new")
  if [ -n "$old" ] && [ "$rc" -eq 0 ] && [ -n "$new" ] && [ "$new" != "$old" ] &&
    [ "$(health_pid "127.0.0.1:$port")" = "$new" ] && ! kill -0 "$old" 2>/dev/null; then
    res[$side.free_move]="PASS moved to 127.0.0.1"
  else res[$side.free_move]="FAIL rc $rc, pid '${old:-none}' -> '${new:-none}'"; fi
  stop "$old" "$new"
}

for side in A B; do
  dir=AB_${side}_DIR port=AB_PORT_$side
  held "$side" "${!dir}" "${!port}" lo 127.0.0.2 127.0.0.1 127.0.0.1
  held "$side" "${!dir}" "${!port}" any 127.0.0.1 127.0.0.2 0.0.0.0
  free "$side" "${!dir}" "${!port}"
done

echo
echo "Issue #142: rerun that changes only the bind, new address already taken (lo: 127.0.0.2 -> 127.0.0.1; any: 127.0.0.1 -> 0.0.0.0)"
printf '%-12s %-36s %-36s %s\n' check A B same
regress=() bfail=()
for c in lo_check lo_config lo_kept any_check any_config any_kept free_move; do
  a=${res[A.$c]:-"FAIL not run"} b=${res[B.$c]:-"FAIL not run"}
  same=NO; [ "$a" = "$b" ] && same=yes
  printf '%-12s %-36s %-36s %s\n' "$c" "${a:0:36}" "${b:0:36}" "$same"
  if [[ "$a" == PASS* && "$b" != PASS* ]]; then regress+=("$c"); fi
  if [[ "$b" != PASS* ]]; then bfail+=("$c"); fi
done
if [ ${#regress[@]} -gt 0 ]; then echo "REGRESSION: passes on A, fails on B: ${regress[*]}"; fi
if [ ${#bfail[@]} -gt 0 ]; then echo "INSTALL FAILURE on B: ${bfail[*]}"; exit 1; fi
exit 0
