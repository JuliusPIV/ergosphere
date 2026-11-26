#!/usr/bin/env bash
# Manual mode offers the current port plus other ports as a lettered list and
# checks the final bind:port before anything changes: a port held by anything
# but this install is named with its holder and port and bind are asked again,
# up to 3 times, then the run stops with nothing changed. Automatic mode still
# fails at the checks with nothing changed (issue #82).
#
# Run: ERGOSPHERE_TEST_PORT=<spare port> bash tests/install_port_menu_test.sh
# Uses that port and the next two. ERGOSPHERE_INSTALL_PORT_OFFERS stands in for
# 8080 8081, so nothing outside those ports is probed. No network: the package
# is served from 127.0.0.1 and grok is a stub that only answers --version. A pty
# plays the terminal. Every server it starts is stopped at the end.
# The case with no --port (it would probe 9241) runs only inside a private
# network namespace (unshare -rn), where that port is not the host's.
set -u
repo=$(cd "$(dirname "$0")/.." && pwd)
mode=${1:-}
port=${ERGOSPHERE_TEST_PORT:-$(python3 -I "$repo/tests/free_port.py" 3)} || exit 1
busy=$((port + 1))
free=$((port + 2))
if [[ "$mode" == --netns ]]; then
  ip link set lo up 2>/dev/null
fi
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

# The package server, and a plain HTTP server (not Ergosphere) on 127.0.0.1:$busy.
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
holder=$!
pids+=("$holder")
for _ in $(seq 50); do [[ -s "$work/www.port" ]] && break; sleep 0.1; done
url="http://127.0.0.1:$(cat "$work/www.port")/src.tar.gz"

inst_env() {
  env -u ERGOSPHERE_PORT -u ERGOSPHERE_CONFIG HOME="$work/u" PATH="$work/bin:$PATH" TMPDIR="$work" \
    ERGOSPHERE_INSTALL_SOURCE_URL="$url" ERGOSPHERE_INSTALL_PORT_OFFERS="$busy $free" "$@"
}

# run_manual PORT ANSWER... : runs install.sh --manual --port PORT (no --port
# for "-") on a pty, typing one answer per prompt (each prompt ends in ": " or
# "] "). Prints everything shown.
run_manual() {
  local p=()
  if [[ "$1" != - ]]; then p=(--port "$1"); fi
  shift
  inst_env python3 -I -c '
import os, pty, select, sys, time
argv, answers = sys.argv[1:sys.argv.index("--")], sys.argv[sys.argv.index("--") + 1:]
pid, fd = pty.fork()
if pid == 0:
    os.execvp(argv[0], argv)
buf, sent, end = b"", 0, time.time() + 60
while time.time() < end:
    if not select.select([fd], [], [], 1)[0]:
        continue
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
if time.time() >= end:
    os.kill(pid, 9)
    buf += b"\n[driver: no answer left, installer killed]\n"
sys.stdout.write(buf.decode(errors="replace").replace("\r", ""))
sys.exit(os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1]))
' bash "$repo/install.sh" --manual --dir "$work/u/app" ${p[@]+"${p[@]}"} -- "$@"
}
field() { sed -n "s/^  $1: *//p" <<<"$2" | head -n 1; }
health_pid() { # PORT [HOST]
  curl --noproxy '*' -s --max-time 3 "http://${2:-127.0.0.1}:$1/health" |
    python3 -I -c 'import json,sys; d=json.load(sys.stdin); print(d["pid"] if d.get("product") == "ergosphere" else "")' 2>/dev/null
}
# Every file under the throwaway HOME (the install is in it), with size,
# mtime and mode. The server's own log is left out: it is not the installer's.
listing() {
  find "$work/u" -path "$work/u/app/ergosphere.log" -prune -o -printf '%P %s %T@ %m\n' | sort
}

if [[ "$mode" == --netns ]]; then
  # A config with no port key; choosing the current port (a) writes it, as
  # typing it did before the list.
  mkdir -p "$work/u/app"
  printf '{"bind": "127.0.0.1", "agents": {}}\n' >"$work/u/app/ergosphere.json"
  out=$(run_manual - "" a "" "")
  pid=$(field pid "$out")
  if [[ -n "$pid" ]]; then pids+=("$pid"); fi
  cfg_port() { python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1])).get("port"))' "$work/u/app/ergosphere.json"; }
  if [[ "$(cfg_port)" == 9241 ]]; then
    ok "choosing the current port 9241 writes the port key"
  else
    bad "port key not written: $(cat "$work/u/app/ergosphere.json")"
    printf '%s\n' "$out" | tail -n 15
  fi
  # Here the installer runs as uid 0 of the namespace, so a typed low port is
  # allowed: 2 means port 2, not an entry of the list.
  out=$(run_manual - "" 2 "" "")
  pid=$(field pid "$out")
  if [[ -n "$pid" ]]; then pids+=("$pid"); fi
  if [[ "$(cfg_port)" == 2 && -n "$pid" && "$(health_pid 2)" == "$pid" ]]; then
    ok "typed port 2 (as root) is port 2"
  else
    bad "typed port 2 (as root): config port $(cfg_port), pid '${pid:-none}'"
    printf '%s\n' "$out" | tail -n 15
  fi
  exit "$fail"
fi

# Entry b (busy) with bind Enter -> warned, naming the holder; then c (free).
rc=0
out=$(run_manual "$port" "" b "" c "" "") || rc=$?
pid=$(field pid "$out")
if [[ -n "$pid" ]]; then pids+=("$pid"); fi
if grep -q "a) $port" <<<"$out" && grep -q "b) $busy" <<<"$out" && grep -q "c) $free" <<<"$out"; then
  ok "menu offers $port, $busy and $free"
else
  bad "menu does not offer $port, $busy and $free"
  printf '%s\n' "$out" | head -n 20
fi
if grep -q "127.0.0.1:$busy is in use by pid $holder (python3)" <<<"$out" && [[ $(grep -c "a) $port" <<<"$out") -eq 2 ]]; then
  ok "busy $busy warns, names pid $holder, and asks again"
else
  bad "busy $busy: no warning naming pid $holder, or not asked again"
  grep -i warn <<<"$out"
fi
if [[ "$rc" -eq 0 && -n "$pid" && "$(health_pid "$free")" == "$pid" ]] && grep -q "^port: $free" <<<"$out"; then
  ok "then free $free: installed and running, pid $pid, port printed"
else
  bad "not running on $free: exit $rc, pid '${pid:-none}'"
  printf '%s\n' "$out" | tail -n 15
fi

# Enter keeps the current port: this install's own server holds it, so no warning.
out=$(run_manual "$free" "" "" "" "")
if [[ -n "$(field pid "$out")" ]]; then pids+=("$(field pid "$out")"); fi
if ! grep -q "in use by" <<<"$out" && [[ "$(field pid "$out")" == "$pid" && "$(field changed "$out")" == nothing* ]]; then
  ok "Enter keeps $free (own server); second run changed nothing"
else
  bad "Enter on rerun: pid '$(field pid "$out")', changed '$(field changed "$out")'"
  printf '%s\n' "$out" | tail -n 15
fi

# $busy is taken on 127.0.0.1 only; with bind 127.0.0.2 it is free.
rc=0
out=$(run_manual "$free" "" b 127.0.0.2 "") || rc=$?
pid=$(field pid "$out")
if [[ -n "$pid" ]]; then pids+=("$pid"); fi
if [[ "$rc" -eq 0 && -n "$pid" && "$(health_pid "$busy" 127.0.0.2)" == "$pid" ]] && ! grep -q "in use by" <<<"$out"; then
  ok "port $busy with bind 127.0.0.2 accepted, pid $pid runs there"
else
  bad "port $busy with bind 127.0.0.2: exit $rc, pid '${pid:-none}'"
  printf '%s\n' "$out" | tail -n 15
fi

# Busy three times (127.0.0.1:$busy) -> stops; files and server untouched.
before=$(listing)
rc=0
out=$(run_manual "$busy" "" "" 127.0.0.1 "" 127.0.0.1 "" 127.0.0.1) || rc=$?
if [[ "$rc" -ne 0 && $(grep -c "^Warning: 127.0.0.1:$busy is in use" <<<"$out") -eq 2 ]] &&
  grep -q "^failed at check: 127.0.0.1:$busy is in use by pid $holder (python3); asked 3 times, nothing was changed" <<<"$out"; then
  ok "busy 3 times: stops (exit $rc) after 2 warnings"
else
  bad "busy 3 times: exit $rc, $(grep -E '^(failed at|Warning)' <<<"$out")"
fi
served=$(health_pid "$busy" 127.0.0.2)
if [[ "$(listing)" == "$before" && -n "$pid" && "$served" == "$pid" ]]; then
  ok "busy 3 times: no file changed, pid $pid still serves"
else
  bad "busy 3 times: pid '${pid:-none}' should still serve 127.0.0.2:$busy (serves '${served:-none}'); file diff: $(diff <(echo "$before") <(listing) | head -n 5)"
fi

# Automatic mode has no question: a busy port still fails at check, nothing changed.
rc=0
out=$(inst_env bash "$repo/install.sh" --auto --dir "$work/u/app2" --port "$busy" --bind 127.0.0.1 </dev/null 2>&1) || rc=$?
if [[ "$rc" -ne 0 ]] && grep -q "^failed at check" <<<"$out" && grep -q "port $busy is in use" <<<"$out" &&
  [[ "$(listing)" == "$before" && "$(health_pid "$busy" 127.0.0.2)" == "$pid" ]]; then
  ok "auto on busy $busy: fails at check (exit $rc), nothing changed"
else
  bad "auto on busy $busy: exit $rc, $(grep '^failed at' <<<"$out")"
fi

# This install's own server moves from 127.0.0.2:$busy to 127.0.0.2:$free, then
# manual mode keeps $free and asks bind 0.0.0.0. The only thing on $free is this
# install's own server, so the port is free and the server moves (#150).
out=$(run_manual "$free" "" "" 127.0.0.2 "") || true
pid=$(field pid "$out")
if [[ -n "$pid" ]]; then pids+=("$pid"); fi
rc=0
out=$(run_manual "$free" "" "" 0.0.0.0 "" y) || rc=$?
new=$(field pid "$out")
if [[ -n "$new" ]]; then pids+=("$new"); fi
if [[ "$rc" -eq 0 && -n "$new" && "$new" != "$pid" && "$(health_pid "$free")" == "$new" ]] && ! grep -q "in use by" <<<"$out"; then
  ok "manual bind 0.0.0.0 on $free moves own server (pid $pid -> $new)"
else
  bad "manual bind 0.0.0.0 on $free: exit $rc, pid '${new:-none}' (was '${pid:-none}')"
  printf "%s\n" "$out" | tail -n 15
fi

if unshare -rn true 2>/dev/null; then
  ERGOSPHERE_TEST_PORT=$port unshare -rn bash "$0" --netns || fail=1
else
  echo "skip: no private network namespace (unshare -rn) for the no --port case"
fi

exit "$fail"
