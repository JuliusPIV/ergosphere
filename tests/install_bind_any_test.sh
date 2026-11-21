#!/usr/bin/env bash
# With bind 0.0.0.0, a program already listening on the port at another
# loopback address (127.0.0.2) must stop the install at check, with nothing
# written (issue #150).  The installer only asked 127.0.0.1, saw it refused,
# called the port free, wrote files and config, then failed at start.
# Also: an Ergosphere from this dir on 127.0.0.2:P still moves to 0.0.0.0:P
# (its own socket must not count as another program).
#
# Run: ERGOSPHERE_TEST_PORT=<spare port> bash tests/install_bind_any_test.sh
# Uses that port and the next two. No network: the package is served from
# 127.0.0.1 and grok is a stub that only answers --version. Every server it
# starts is stopped at the end.
set -u
repo=$(cd "$(dirname "$0")/.." && pwd)
port=${ERGOSPHERE_TEST_PORT:-$(python3 -I "$repo/tests/free_port.py" 3)} || exit 1
own=$((port + 1))
moved_own=$((port + 2))
work=$(mktemp -d "${TMPDIR:-/tmp}/ergosphere-bind-any-test.XXXXXX")
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

# 1. Another program on 127.0.0.2:$port; install with bind 0.0.0.0.
python3 -I -c '
import http.server, sys
class Quiet(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self): self.send_error(404)
s = http.server.HTTPServer(("127.0.0.2", int(sys.argv[1])), Quiet)
open(sys.argv[2], "w").write("up")
s.serve_forever()' "$port" "$work/other.up" </dev/null >/dev/null 2>&1 &
pids+=($!)
for _ in $(seq 50); do [[ -s "$work/other.up" ]] && break; sleep 0.1; done
[[ -s "$work/other.up" ]] || { bad "could not start the other program on 127.0.0.2:$port"; exit 1; }

rc=0
out=$(run_install "$work/u/app" --bind 0.0.0.0 --port "$port") || rc=$?
if [[ "$rc" -ne 0 ]] && grep -q '^failed at check' <<<"$out"; then
  ok "bind 0.0.0.0 with $port held on 127.0.0.2: failed at check (exit $rc)"
else
  bad "bind 0.0.0.0 with $port held on 127.0.0.2: exit $rc, want failed at check"
  printf '%s\n' "$out" | tail -n 15
fi
if [[ ! -e "$work/u/app" ]]; then
  ok "nothing written (no install dir, so no files or config)"
else
  bad "files written: $(cd "$work/u" && find . -mindepth 1 -maxdepth 4 | tr '\n' ' ')"
fi
if curl --noproxy '*' -s -o /dev/null -w '%{http_code}' "http://127.0.0.2:$port/" | grep -q 404; then
  ok "the program on 127.0.0.2:$port was not touched"
else
  bad "the program on 127.0.0.2:$port no longer answers"
fi

# 2. This install's own server on 127.0.0.2:$own moves to 0.0.0.0:$own.
out=$(run_install "$work/u/own" --bind 127.0.0.2 --port "$own")
old=$(field pid "$out")
if [[ -n "$old" ]]; then pids+=("$old"); fi
if [[ -n "$old" && "$(health_pid "127.0.0.2:$own")" == "$old" ]]; then
  ok "own install runs pid $old on 127.0.0.2:$own"
else
  bad "own install did not start on 127.0.0.2:$own"
  printf '%s\n' "$out" | tail -n 15
fi
rc=0
out=$(run_install "$work/u/own" --bind 0.0.0.0 --port "$own") || rc=$?
moved=$(field pid "$out")
if [[ -n "$moved" ]]; then pids+=("$moved"); fi
if [[ "$rc" -eq 0 && -n "$moved" && "$moved" != "$old" && "$(health_pid "127.0.0.1:$own")" == "$moved" ]] &&
  ! kill -0 "$old" 2>/dev/null; then
  ok "own server moved from 127.0.0.2:$own to 0.0.0.0:$own (pid $old -> $moved)"
else
  bad "move to 0.0.0.0:$own: exit $rc, pid '${moved:-none}', old pid $old $(kill -0 "$old" 2>/dev/null && echo alive || echo gone)"
  printf '%s\n' "$out" | tail -n 15
fi

# 3. This install's own server runs on 127.0.0.2:$moved_own, but settings
# changed the saved bind to 127.0.0.1 (no restart). Bind 0.0.0.0 must still
# find that server and move it, not call the port another program's (#150).
out=$(run_install "$work/u/cfgmove" --bind 127.0.0.2 --port "$moved_own")
old=$(field pid "$out")
if [[ -n "$old" ]]; then pids+=("$old"); fi
python3 -I -c '
import json, sys
p = sys.argv[1] + "/ergosphere.json"
d = json.load(open(p))
d["bind"] = "127.0.0.1"
json.dump(d, open(p, "w"))' "$work/u/cfgmove" </dev/null
rc=0
out=$(run_install "$work/u/cfgmove" --bind 0.0.0.0 --port "$moved_own") || rc=$?
moved=$(field pid "$out")
if [[ -n "$moved" ]]; then pids+=("$moved"); fi
if [[ "$rc" -eq 0 && -n "$old" && -n "$moved" && "$moved" != "$old" && "$(health_pid "127.0.0.1:$moved_own")" == "$moved" ]] &&
  ! kill -0 "$old" 2>/dev/null; then
  ok "saved bind 127.0.0.1 but server on 127.0.0.2:$moved_own: moved to 0.0.0.0 (pid $old -> $moved)"
else
  bad "saved-bind move to 0.0.0.0:$moved_own: exit $rc, old pid '${old:-none}', pid '${moved:-none}'"
  printf '%s\n' "$out" | tail -n 15
fi

exit "$fail"
