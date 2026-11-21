#!/usr/bin/env bash
# A rerun that changes only the bind (same port) must check the new address
# (issue #142).  With this install's server on 127.0.0.2:P and another
# program on 127.0.0.1:P, a rerun with --bind 127.0.0.1 printed "it moves to
# 127.0.0.1:P", rewrote ergosphere.json, then failed at start.  It must fail
# at check with the config unchanged and both servers left running.
# Also: a bind-only move with the new address free still moves the server
# (127.0.0.2 -> 127.0.0.1, and 0.0.0.0 -> 127.0.0.1, where the new address
# answers as the same server), reports it once, and a second run changes
# nothing; a bind-only move to 0.0.0.0 while another program holds the port
# on 127.0.0.2 also fails at check.
#
# Run: ERGOSPHERE_TEST_PORT=<spare port> bash tests/install_bind_move_test.sh
# Uses that port and the next two. No network: the package is served from
# 127.0.0.1 and grok is a stub that only answers --version. Every server it
# starts is stopped at the end.
set -u
repo=$(cd "$(dirname "$0")/.." && pwd)
port=${ERGOSPHERE_TEST_PORT:-$(python3 -I "$repo/tests/free_port.py" 3)} || exit 1
free=$((port + 1))
wild=$((port + 2))
work=$(mktemp -d "${TMPDIR:-/tmp}/ergosphere-bind-move-test.XXXXXX")
pids=()
fail=0

# shellcheck disable=SC2317  # run by the EXIT trap
cleanup() {
  local p deadline=$((SECONDS + 10))
  for p in ${pids[@]+"${pids[@]}"}; do kill "$p" 2>/dev/null; done
  # A server takes about 0.5 s to exit after TERM; wait, so a run started
  # right after this one finds its ports free.
  for p in ${pids[@]+"${pids[@]}"}; do
    while kill -0 "$p" 2>/dev/null && (( SECONDS < deadline )); do sleep 0.1; done
  done
  rm -rf -- "$work"
}
trap cleanup EXIT

ok() { echo "ok:   $1"; }
bad() { echo "FAIL: $1"; fail=1; }

mkdir -p "$work/pkg/ergosphere-test" "$work/bin" "$work/www" "$work/u"
cp -p "$repo/ergosphere.py" "$repo/LICENSE" "$work/pkg/ergosphere-test/"
cp -rp "$repo/vendor" "$work/pkg/ergosphere-test/"
tar -czf "$work/www/src.tar.gz" -C "$work/pkg" ergosphere-test
printf '#!/bin/sh\necho "grok 0.0.0-test"\n' >"$work/bin/grok"
chmod +x "$work/bin/grok"

python3 -I -c '
import functools, http.server, sys
class Pkg(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a): pass
s = http.server.HTTPServer(("127.0.0.1", 0), functools.partial(Pkg, directory=sys.argv[1]))
open(sys.argv[2], "w").write(str(s.server_address[1]))
s.serve_forever()' "$work/www" "$work/www.port" </dev/null >/dev/null 2>&1 &
pids+=($!)
for _ in $(seq 50); do [[ -s "$work/www.port" ]] && break; sleep 0.1; done
url="http://127.0.0.1:$(cat "$work/www.port")/src.tar.gz"

run_install() {
  local dir=$1
  shift
  env -u ERGOSPHERE_PORT -u ERGOSPHERE_CONFIG HOME="$work/u" PATH="$work/bin:$PATH" TMPDIR="$work" \
    ERGOSPHERE_INSTALL_SOURCE_URL="$url" bash "$repo/install.sh" --auto --yes --dir "$dir" "$@" </dev/null 2>&1
}
field() { sed -n "s/^  $1: *//p" <<<"$2" | head -n 1; }
health_pid() {
  curl --noproxy '*' -s --max-time 3 "http://$1/health" |
    python3 -I -c 'import json,sys; d=json.load(sys.stdin); print(d["pid"] if d.get("product") == "ergosphere" else "")' 2>/dev/null
}
moves_rows() { grep -c 'it moves to' <<<"$1"; }

# 1. Own server on 127.0.0.2:$port, another program on 127.0.0.1:$port,
# rerun with --bind 127.0.0.1.
out=$(run_install "$work/u/held" --bind 127.0.0.2 --port "$port")
old=$(field pid "$out")
if [[ -n "$old" ]]; then pids+=("$old"); fi
if [[ -n "$old" && "$(health_pid "127.0.0.2:$port")" == "$old" ]]; then
  ok "own install runs pid $old on 127.0.0.2:$port"
else
  bad "own install did not start on 127.0.0.2:$port"
  printf '%s\n' "$out" | tail -n 15
fi
python3 -I -c '
import http.server, sys
class Quiet(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self): self.send_error(404)
s = http.server.HTTPServer(("127.0.0.1", int(sys.argv[1])), Quiet)
open(sys.argv[2], "w").write("up")
s.serve_forever()' "$port" "$work/other.up" </dev/null >/dev/null 2>&1 &
pids+=($!)
for _ in $(seq 50); do [[ -s "$work/other.up" ]] && break; sleep 0.1; done
[[ -s "$work/other.up" ]] || { bad "could not start the other program on 127.0.0.1:$port"; exit 1; }
before=$(cat "$work/u/held/ergosphere.json")
rc=0
out=$(run_install "$work/u/held" --bind 127.0.0.1 --port "$port") || rc=$?
if [[ "$rc" -ne 0 ]] && grep -q '^failed at check' <<<"$out"; then
  ok "bind-only move onto a taken 127.0.0.1:$port: failed at check (exit $rc)"
else
  bad "bind-only move onto a taken 127.0.0.1:$port: exit $rc, want failed at check"
  printf '%s\n' "$out" | tail -n 15
fi
if [[ "$(cat "$work/u/held/ergosphere.json")" == "$before" ]]; then
  ok "config left as it was (bind still 127.0.0.2)"
else
  bad "config changed: $(cat "$work/u/held/ergosphere.json")"
fi
if [[ -n "$old" && "$(health_pid "127.0.0.2:$port")" == "$old" ]]; then
  ok "own server pid $old still serves 127.0.0.2:$port"
else
  bad "own server pid ${old:-none} no longer serves 127.0.0.2:$port"
fi
if curl --noproxy '*' -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$port/" | grep -q 404; then
  ok "the program on 127.0.0.1:$port was not touched"
else
  bad "the program on 127.0.0.1:$port no longer answers"
fi

# 2. Bind-only move with the new address free: 127.0.0.2:$free -> 127.0.0.1:$free,
# then the same rerun again changes nothing.
out=$(run_install "$work/u/free" --bind 127.0.0.2 --port "$free")
old=$(field pid "$out")
if [[ -n "$old" ]]; then pids+=("$old"); fi
rc=0
out=$(run_install "$work/u/free" --bind 127.0.0.1 --port "$free") || rc=$?
new=$(field pid "$out")
if [[ -n "$new" ]]; then pids+=("$new"); fi
if [[ "$rc" -eq 0 && -n "$old" && -n "$new" && "$new" != "$old" && "$(health_pid "127.0.0.1:$free")" == "$new" ]] &&
  ! kill -0 "$old" 2>/dev/null && [[ "$(moves_rows "$out")" -eq 1 ]]; then
  ok "free 127.0.0.1:$free: own server moved (pid $old -> $new), reported once"
else
  bad "move to free 127.0.0.1:$free: exit $rc, pid '${old:-none}' -> '${new:-none}', $(moves_rows "$out") move rows"
  printf '%s\n' "$out" | tail -n 15
fi
rc=0
out=$(run_install "$work/u/free" --bind 127.0.0.1 --port "$free") || rc=$?
if [[ "$rc" -eq 0 && -n "$new" ]] && grep -q "start: already running, pid $new" <<<"$out"; then
  ok "second run changed nothing (pid $new kept)"
else
  bad "second run: exit $rc, want already running pid ${new:-none}"
  printf '%s\n' "$out" | tail -n 15
fi

# 3. The server from step 2 runs on 127.0.0.1:$free; another program takes
# 127.0.0.2:$free. A bind-only move to 0.0.0.0 must stop at check: this
# install's socket on the port must not hide the other program's.
python3 -I -c '
import http.server, sys
class Quiet(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self): self.send_error(404)
s = http.server.HTTPServer(("127.0.0.2", int(sys.argv[1])), Quiet)
open(sys.argv[2], "w").write("up")
s.serve_forever()' "$free" "$work/wild-other.up" </dev/null >/dev/null 2>&1 &
pids+=($!)
for _ in $(seq 50); do [[ -s "$work/wild-other.up" ]] && break; sleep 0.1; done
[[ -s "$work/wild-other.up" ]] || { bad "could not start the other program on 127.0.0.2:$free"; exit 1; }
before=$(cat "$work/u/free/ergosphere.json")
rc=0
out=$(run_install "$work/u/free" --bind 0.0.0.0 --port "$free") || rc=$?
if [[ "$rc" -ne 0 ]] && grep -q '^failed at check' <<<"$out" && [[ "$(cat "$work/u/free/ergosphere.json")" == "$before" ]]; then
  ok "move to 0.0.0.0:$free with 127.0.0.2:$free taken: failed at check, config unchanged"
else
  bad "move to 0.0.0.0:$free with 127.0.0.2:$free taken: exit $rc, want failed at check with config unchanged"
  printf '%s\n' "$out" | tail -n 15
fi
if [[ -n "$new" && "$(health_pid "127.0.0.1:$free")" == "$new" ]]; then
  ok "own server pid $new still serves 127.0.0.1:$free"
else
  bad "own server pid ${new:-none} no longer serves 127.0.0.1:$free"
fi

# 4. 0.0.0.0:$wild -> 127.0.0.1:$wild: the new address answers as the same
# server, which must not block the move or be reported twice.
out=$(run_install "$work/u/wild" --bind 0.0.0.0 --port "$wild")
old=$(field pid "$out")
if [[ -n "$old" ]]; then pids+=("$old"); fi
rc=0
out=$(run_install "$work/u/wild" --bind 127.0.0.1 --port "$wild") || rc=$?
new=$(field pid "$out")
if [[ -n "$new" ]]; then pids+=("$new"); fi
if [[ "$rc" -eq 0 && -n "$old" && -n "$new" && "$new" != "$old" && "$(health_pid "127.0.0.1:$wild")" == "$new" ]] &&
  ! kill -0 "$old" 2>/dev/null && [[ "$(moves_rows "$out")" -eq 1 ]]; then
  ok "0.0.0.0:$wild -> 127.0.0.1:$wild: moved (pid $old -> $new), reported once"
else
  bad "move 0.0.0.0 -> 127.0.0.1:$wild: exit $rc, pid '${old:-none}' -> '${new:-none}', $(moves_rows "$out") move rows"
  printf '%s\n' "$out" | tail -n 15
fi

exit "$fail"
