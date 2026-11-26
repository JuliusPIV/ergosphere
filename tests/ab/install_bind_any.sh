#!/bin/bash
# A/B section for issue #150: install.sh with bind 0.0.0.0 when the port is
# held on another loopback address.  Each side's install.sh, on that side's
# port, with a throwaway HOME and the B tree's ergosphere.py as the package:
# 1. another program on 127.0.0.2:P, install --bind 0.0.0.0 --port P;
# 2. that program stopped, install --bind 127.0.0.2, then rerun --bind 0.0.0.0
#    (this install's own server must still move).
# Prints the section for summary.txt; exit 1 when any check fails on B.
# Needs AB_A_DIR, AB_B_DIR, AB_PORT_A, AB_PORT_B, AB_OUT (see run.sh).
set -u
w="$AB_OUT/bind-any"
rm -rf "$w"; mkdir -p "$w/www" "$w/pkg/ergosphere-ab" "$w/bin" || exit 2
log() { printf '%s install_bind_any.sh: %s\n' "$(date +%T)" "$*" >&2; }
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
declare -A res
side_run() { # side code-dir port
  local side=$1 code=$2 port=$3 h="$w/$1" out rc other old new
  mkdir -p "$h"
  run() {
    local dir=$1; shift
    env -u ERGOSPHERE_PORT -u ERGOSPHERE_CONFIG HOME="$h" PATH="$w/bin:$PATH" TMPDIR="$w" \
      ERGOSPHERE_INSTALL_SOURCE_URL="$url" bash "$code/install.sh" --auto --yes --dir "$dir" "$@" </dev/null 2>&1
  }
  python3 -I -c '
import http.server, sys
class Quiet(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self): self.send_error(404)
s = http.server.HTTPServer(("127.0.0.2", int(sys.argv[1])), Quiet)
open(sys.argv[2], "w").write("up")
s.serve_forever()' "$port" "$h/other.up" </dev/null >/dev/null 2>&1 &
  other=$!; pids+=("$other")
  for _ in $(seq 100); do [ -s "$h/other.up" ] && break; sleep 0.1; done
  if [ ! -s "$h/other.up" ]; then
    res[$side.check_stage]="FAIL no listener on 127.0.0.2"; return
  fi
  log "$side: 127.0.0.2:$port held; install --bind 0.0.0.0 with $code/install.sh"
  rc=0; out=$(run "$h/app" --bind 0.0.0.0 --port "$port") || rc=$?
  printf '%s\n' "$out" >"$h/install-held.log"
  res[$side.check_stage]="FAIL rc $rc, $(grep -o '^failed at [a-z]*' <<<"$out" || echo 'no failure')"
  if [ "$rc" -ne 0 ] && grep -q '^failed at check' <<<"$out"; then res[$side.check_stage]="PASS failed at check"; fi
  if [ -e "$h/app" ]; then res[$side.nothing_written]="FAIL $(find "$h/app" -type f | wc -l) files in install dir"
  else res[$side.nothing_written]="PASS no install dir"; fi
  if curl --noproxy '*' -s -o /dev/null -w '%{http_code}' "http://127.0.0.2:$port/" | grep -q 404; then
    res[$side.other_untouched]="PASS still answers"
  else res[$side.other_untouched]="FAIL no answer"; fi
  kill "$other" 2>/dev/null
  for _ in $(seq 50); do kill -0 "$other" 2>/dev/null || break; sleep 0.1; done

  log "$side: own server on 127.0.0.2:$port, then rerun --bind 0.0.0.0"
  out=$(run "$h/own" --bind 127.0.0.2 --port "$port"); printf '%s\n' "$out" >"$h/install-own.log"
  old=$(field pid "$out"); [ -n "$old" ] && pids+=("$old")
  rc=0; out=$(run "$h/own" --bind 0.0.0.0 --port "$port") || rc=$?
  printf '%s\n' "$out" >"$h/install-move.log"
  new=$(field pid "$out"); [ -n "$new" ] && pids+=("$new")
  if [ -n "$old" ] && [ "$rc" -eq 0 ] && [ -n "$new" ] && [ "$new" != "$old" ] &&
    [ "$(health_pid "127.0.0.1:$port")" = "$new" ] && ! kill -0 "$old" 2>/dev/null; then
    res[$side.own_move]="PASS moved to 0.0.0.0"
  else res[$side.own_move]="FAIL rc $rc, pid '${old:-none}' -> '${new:-none}'"; fi
  for p in $old $new; do kill "$p" 2>/dev/null; done
  for _ in $(seq 50); do kill -0 "${new:-0}" 2>/dev/null || break; sleep 0.1; done
}

side_run A "$AB_A_DIR" "$AB_PORT_A"
side_run B "$AB_B_DIR" "$AB_PORT_B"

echo
echo "Issue #150: install --bind 0.0.0.0 while another program holds the port on 127.0.0.2"
printf '%-16s %-36s %-36s %s\n' check A B same
regress=() bfail=()
for c in check_stage nothing_written other_untouched own_move; do
  a=${res[A.$c]:-"FAIL not run"} b=${res[B.$c]:-"FAIL not run"}
  same=NO; [ "$a" = "$b" ] && same=yes
  printf '%-16s %-36s %-36s %s\n' "$c" "${a:0:36}" "${b:0:36}" "$same"
  if [[ "$a" == PASS* && "$b" != PASS* ]]; then regress+=("$c"); fi
  if [[ "$b" != PASS* ]]; then bfail+=("$c"); fi
done
if [ ${#regress[@]} -gt 0 ]; then echo "REGRESSION: passes on A, fails on B: ${regress[*]}"; fi
if [ ${#bfail[@]} -gt 0 ]; then echo "INSTALL FAILURE on B: ${bfail[*]}"; exit 1; fi
exit 0
