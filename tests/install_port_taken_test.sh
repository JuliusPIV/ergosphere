#!/usr/bin/env bash
# A rerun that moves the server to a new port must not stop the old server
# when something else took the new port after the install checks (issue #62).
# The package server occupies the new port while it serves the download, so
# the port is free at "Checking install target" and taken at start.
#
# Run: ERGOSPHERE_TEST_PORT=<spare port> bash tests/install_port_taken_test.sh
# Uses that port and the next one. No network: the package is served from
# 127.0.0.1 and grok is a stub that only answers --version. Every server it
# starts is stopped at the end.
set -u
repo=$(cd "$(dirname "$0")/.." && pwd)
port=${ERGOSPHERE_TEST_PORT:-$(python3 -I "$repo/tests/free_port.py" 3)}
new=$((port + 1))
work=$(mktemp -d "${TMPDIR:-/tmp}/ergosphere-port-taken-test.XXXXXX")
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

# Serves the package. While $work/occupy holds a port, each download first
# starts a plain HTTP server (404 for everything, not Ergosphere) on it.
python3 -I -c '
import functools, http.server, os, sys, threading
www, portfile, flag = sys.argv[1:4]
held = {}
class Pkg(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        if os.path.exists(flag):
            p = int(open(flag).read())
            if p not in held:
                o = http.server.HTTPServer(("127.0.0.1", p), Quiet)
                threading.Thread(target=o.serve_forever, daemon=True).start()
                held[p] = o
        super().do_GET()
class Quiet(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self): self.send_error(404)
s = http.server.HTTPServer(("127.0.0.1", 0), functools.partial(Pkg, directory=www))
open(portfile, "w").write(str(s.server_address[1]))
s.serve_forever()' "$work/www" "$work/www.port" "$work/occupy" </dev/null >/dev/null 2>&1 &
pids+=($!)
for _ in $(seq 50); do [[ -s "$work/www.port" ]] && break; sleep 0.1; done
url="http://127.0.0.1:$(cat "$work/www.port")/src.tar.gz"

run_install() {
  env -u ERGOSPHERE_PORT -u ERGOSPHERE_CONFIG HOME="$work/u" PATH="$work/bin:$PATH" TMPDIR="$work" \
    ERGOSPHERE_INSTALL_SOURCE_URL="$url" bash "$repo/install.sh" --auto --dir "$work/u/app" "$@" </dev/null 2>&1
}
field() { sed -n "s/^  $1: *//p" <<<"$2" | head -n 1; }
health_pid() {
  curl --noproxy '*' -s --max-time 3 "http://127.0.0.1:$1/health" |
    python3 -I -c 'import json,sys; d=json.load(sys.stdin); print(d["pid"] if d.get("product") == "ergosphere" else "")' 2>/dev/null
}

out=$(run_install --port "$port")
old=$(field pid "$out")
if [[ -n "$old" ]]; then pids+=("$old"); fi
if [[ -n "$old" && "$(health_pid "$port")" == "$old" ]]; then
  ok "first install runs pid $old on $port"
else
  bad "first install did not start a server on $port"
  printf '%s\n' "$out" | tail -n 15
  exit 1
fi

echo "$new" >"$work/occupy"
rc=0
out=$(run_install --port "$new") || rc=$?
if [[ "$rc" -ne 0 ]] && grep -q "port $new is free" <<<"$out" && grep -q '^failed at start' <<<"$out"; then
  ok "rerun to taken port $new: checks saw it free, start failed (exit $rc)"
else
  bad "rerun to taken port $new: exit $rc, want a start failure after a free check"
  printf '%s\n' "$out" | tail -n 15
fi
if kill -0 "$old" 2>/dev/null && [[ "$(health_pid "$port")" == "$old" ]]; then
  ok "old server pid $old still serves $port"
else
  bad "old server pid $old was stopped; nothing serves $port"
fi
if grep -q "pid $old" <<<"$(grep '^failed at start' <<<"$out")"; then
  ok "failure names the kept pid $old"
else
  bad "failure does not name the kept pid: $(grep '^failed at' <<<"$out")"
fi
if curl --noproxy '*' -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$new/" | grep -q 404; then
  ok "the program holding $new was not touched"
else
  bad "the program holding $new no longer answers"
fi

# A free new port still moves the server, and a second run changes nothing.
free=$((port + 2))
out=$(run_install --port "$free")
moved=$(field pid "$out")
if [[ -n "$moved" ]]; then pids+=("$moved"); fi
if [[ -n "$moved" && "$moved" != "$old" && "$(health_pid "$free")" == "$moved" ]] && ! kill -0 "$old" 2>/dev/null; then
  ok "rerun to free port $free moved the server (pid $old -> $moved)"
else
  bad "rerun to free port $free: pid '${moved:-none}', old pid $old $(kill -0 "$old" 2>/dev/null && echo alive || echo gone)"
  printf '%s\n' "$out" | tail -n 15
fi
out=$(run_install --port "$free")
if [[ "$(field pid "$out")" == "$moved" && "$(field changed "$out")" == nothing* ]]; then
  ok "second run on $free changed nothing"
else
  bad "second run on $free: pid '$(field pid "$out")', changed '$(field changed "$out")'"
fi

exit "$fail"
