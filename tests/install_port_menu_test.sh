#!/usr/bin/env bash
# Manual mode offers the current port plus other ports as a numbered list,
# prints the chosen port, and warns (and asks again) when a chosen port is
# answered by something that is not Ergosphere (issue #82).
#
# Run: ERGOSPHERE_TEST_PORT=<spare port> bash tests/install_port_menu_test.sh
# Uses that port and the next two. ERGOSPHERE_INSTALL_PORT_OFFERS stands in for
# 8080 8081, so nothing outside those ports is probed. No network: the package
# is served from 127.0.0.1 and grok is a stub that only answers --version. A pty
# plays the terminal. Every server it starts is stopped at the end.
set -u
repo=$(cd "$(dirname "$0")/.." && pwd)
port=${ERGOSPHERE_TEST_PORT:-9560}
busy=$((port + 1))
free=$((port + 2))
work=$(mktemp -d "${TMPDIR:-/tmp}/ergosphere-port-menu-test.XXXXXX")
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

# The package server, and a plain HTTP server (not Ergosphere) on $busy.
python3 -I -c '
import functools, http.server, sys, threading
www, portfile, busy = sys.argv[1], sys.argv[2], int(sys.argv[3])
class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a): pass
o = http.server.HTTPServer(("127.0.0.1", busy), functools.partial(Quiet, directory=www + "/none"))
threading.Thread(target=o.serve_forever, daemon=True).start()
s = http.server.HTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=www))
open(portfile, "w").write(str(s.server_address[1]))
s.serve_forever()' "$work/www" "$work/www.port" "$busy" </dev/null >/dev/null 2>&1 &
pids+=($!)
for _ in $(seq 50); do [[ -s "$work/www.port" ]] && break; sleep 0.1; done
url="http://127.0.0.1:$(cat "$work/www.port")/src.tar.gz"

# run_manual PORT ANSWER... : runs install.sh --manual --port PORT on a pty, typing one answer
# per prompt (each prompt ends in ": " or "] "). Prints everything shown.
run_manual() {
  local p=$1
  shift
  env -u ERGOSPHERE_PORT -u ERGOSPHERE_CONFIG HOME="$work/u" PATH="$work/bin:$PATH" TMPDIR="$work" \
    ERGOSPHERE_INSTALL_SOURCE_URL="$url" ERGOSPHERE_INSTALL_PORT_OFFERS="$busy $free" \
    python3 -I -c '
import os, pty, sys, time
argv, answers = sys.argv[1:sys.argv.index("--")], sys.argv[sys.argv.index("--") + 1:]
pid, fd = pty.fork()
if pid == 0:
    os.execvp(argv[0], argv)
buf, sent, end = b"", 0, time.time() + 120
while time.time() < end:
    try:
        b = os.read(fd, 4096)
    except OSError:
        break
    if not b:
        break
    buf += b
    if sent < len(answers) and buf.rstrip(b" ").endswith((b":", b"]")) and buf.endswith(b" "):
        os.write(fd, answers[sent].encode() + b"\n")
        sent += 1
        buf += b"<" + answers[sent - 1].encode() + b">\n"
sys.stdout.write(buf.decode(errors="replace").replace("\r", ""))
sys.exit(os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1]))
' bash "$repo/install.sh" --manual --dir "$work/u/app" --port "$p" -- "$@"
}
field() { sed -n "s/^  $1: *//p" <<<"$2" | head -n 1; }
health_pid() {
  curl --noproxy '*' -s --max-time 3 "http://127.0.0.1:$1/health" |
    python3 -I -c 'import json,sys; d=json.load(sys.stdin); print(d["pid"] if d.get("product") == "ergosphere" else "")' 2>/dev/null
}

# Dir Enter, port 2 (busy) -> warned and asked again, port 3 (free),
# bind Enter, agent Enter.
rc=0
out=$(run_manual "$port" "" 2 3 "" "") || rc=$?
pid=$(field pid "$out")
if [[ -n "$pid" ]]; then pids+=("$pid"); fi
if grep -q "1) $port" <<<"$out" && grep -q "2) $busy" <<<"$out" && grep -q "3) $free" <<<"$out"; then
  ok "menu offers $port, $busy and $free"
else
  bad "menu does not offer $port, $busy and $free"
  printf '%s\n' "$out" | head -n 20
fi
if grep -q "something already answers on port $busy" <<<"$out" && [[ $(grep -c "1) $port" <<<"$out") -eq 2 ]]; then
  ok "choosing busy $busy warns and asks again"
else
  bad "choosing busy $busy did not warn and ask again"
fi
if grep -q "^port: $free" <<<"$out"; then
  ok "prints the chosen port $free"
else
  bad "chosen port $free not printed"
fi
if [[ "$rc" -eq 0 && -n "$pid" && "$(health_pid "$free")" == "$pid" ]]; then
  ok "installed and running on $free, pid $pid (exit $rc)"
else
  bad "not running on $free: exit $rc, pid '${pid:-none}'"
  printf '%s\n' "$out" | tail -n 15
fi
if curl --noproxy '*' -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$busy/" | grep -q 404; then
  ok "the program holding $busy was not touched"
else
  bad "the program holding $busy no longer answers"
fi

# Enter keeps the current port (now $free), with no warning; nothing changes.
out=$(run_manual "$free" "" "" "" "") || rc=$?
if [[ -n "$(field pid "$out")" ]]; then pids+=("$(field pid "$out")"); fi
if grep -q "^port: $free" <<<"$out" && ! grep -q "already answers" <<<"$out" &&
  [[ "$(field pid "$out")" == "$pid" && "$(field changed "$out")" == nothing* ]]; then
  ok "Enter keeps $free; second run changed nothing"
else
  bad "Enter on rerun: pid '$(field pid "$out")', changed '$(field changed "$out")'"
  printf '%s\n' "$out" | tail -n 15
fi

exit "$fail"
