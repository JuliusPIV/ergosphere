#!/usr/bin/env bash
# The installer restarts a server only when it has no grok sessions, and a
# session that starts after that decision must not be killed (issue #63).
# A curl stub runs one hook right before the installer's last /health probe
# ahead of the restart (the second /health call once the new files are in
# place), so each case lands at the same point every run:
#   1. launch: a grok session starts there -> the server is kept, the grok
#      keeps running;
#   2. end:    the only session ends there -> the server is restarted;
#   3. restart: a Settings restart re-execs the server there (same pid) ->
#      the server is restarted.
# A Quit in progress is not a case here: the server counts a stopping
# session as busy, the same as a running one.
#
# Run: ERGOSPHERE_TEST_PORT=<spare port> bash tests/install_restart_gate_test.sh
# ERGOSPHERE_TEST_INSTALLER=<path> runs another install.sh on this tree's
# packages (the A/B run uses it for the base side).
# No network, no git: packages are served from 127.0.0.1 and grok is a stub
# that answers --version and otherwise sleeps. Every process it starts is
# stopped at the end.
set -u
repo=$(cd "$(dirname "$0")/.." && pwd)
port=${ERGOSPHERE_TEST_PORT:-$(python3 -I "$repo/tests/free_port.py" 3)} || exit 1
installer=${ERGOSPHERE_TEST_INSTALLER:-$repo/install.sh}
work=$(mktemp -d "${TMPDIR:-/tmp}/ergosphere-gate-test.XXXXXX")
home="$work/u" dest="$work/u/app"
real_curl=$(command -v curl) || { echo "FAIL: no curl"; exit 1; }
pids=()
fail=0

# shellcheck disable=SC2317  # run by the EXIT trap
cleanup() {
  local p
  if [[ -s "$work/grok.pid" ]]; then kill "$(cat "$work/grok.pid")" 2>/dev/null; fi
  for p in ${pids[@]+"${pids[@]}"}; do kill "$p" 2>/dev/null; done
  for _ in $(seq 100); do
    for p in ${pids[@]+"${pids[@]}"}; do kill -0 "$p" 2>/dev/null && break; p=""; done
    [[ -z "${p:-}" ]] && break
    sleep 0.1
  done
  for p in ${pids[@]+"${pids[@]}"}; do kill -9 "$p" 2>/dev/null && echo "note: pid $p ignored SIGTERM, killed"; done
  rm -rf -- "$work"
}
trap cleanup EXIT

ok() { echo "ok:   $1"; }
bad() { echo "FAIL: $1"; fail=1; }

# pack NAME: a release archive whose ergosphere.py has its own fingerprint.
pack() {
  local d="$work/pkg-$1/ergosphere-test"
  mkdir -p "$d"
  cp -p "$repo/ergosphere.py" "$repo/LICENSE" "$d/"
  cp -rp "$repo/vendor" "$d/"
  printf '# install_restart_gate_test: %s\n' "$1" >>"$d/ergosphere.py"
  tar -czf "$work/www/$1.tar.gz" -C "$work/pkg-$1" ergosphere-test
}
mkdir -p "$work/bin" "$work/www" "$home"
for v in v1 v2 v3 v4; do pack "$v"; done
cat >"$work/bin/grok" <<'EOF'
#!/bin/sh
for a in "$@"; do
  case "$a" in
    --version) echo "grok 0.0.0-test"; exit 0 ;;
    models) exit 0 ;;
  esac
done
exec sleep 600
EOF
chmod +x "$work/bin/grok"

# The curl stub. With $work/hook holding "ACTION SHA" and the installed
# ergosphere.py at SHA, the second /health request runs ACTION first, once.
cat >"$work/bin/curl" <<EOF
#!/usr/bin/env bash
real=$(printf '%q' "$real_curl") work=$(printf '%q' "$work") dest=$(printf '%q' "$dest") port=$port
EOF
cat >>"$work/bin/curl" <<'EOF'
url="http://127.0.0.1:$port"
c() { "$real" --noproxy '*' -s --max-time 5 "$@"; }
health() { c "$url/health" | python3 -I -c "import json,sys; print(json.load(sys.stdin)[sys.argv[1]])" "$1" 2>/dev/null; }
running() { c "$url/session-info" | python3 -I -c "import json,sys; print(json.load(sys.stdin)['running'])" 2>/dev/null; }
post() { c -H "Origin: $url" -H 'Content-Type: application/json' --data "$2" "$url$1"; }
hook() {
  local action=$1 srv started i
  srv=$(health pid)
  case "$action" in
    launch)
      post /launch "{\"approval\":\"ask\",\"cwd\":\"$work\"}" >"$work/hook.out"
      for i in $(seq 100); do
        pgrep -P "$srv" >"$work/grok.pid" && [[ "$(running)" == 1 ]] && break
        sleep 0.1
      done ;;
    end)
      kill "$(cat "$work/grok.pid")"
      for i in $(seq 100); do [[ "$(running)" == 0 ]] && break; sleep 0.1; done ;;
    restart)
      started=$(health started)
      post /settings '{"action":"restart"}' >"$work/hook.out"
      for i in $(seq 150); do
        s=$(health started) && [[ -n "$s" && "$s" != "$started" ]] && break
        sleep 0.1
      done ;;
  esac
  echo "$action done after $i polls" >>"$work/hook.log"
}
if [[ -s "$work/hook" && " $* " == *"/health "* ]]; then
  read -r action sha <"$work/hook"
  if [[ "$(sha256sum <"$dest/ergosphere.py" | cut -d' ' -f1)" == "$sha" ]]; then
    n=$(( $(cat "$work/hook.n" 2>/dev/null || echo 0) + 1 ))
    echo "$n" >"$work/hook.n"
    if [[ "$n" -eq 2 ]]; then rm -f "$work/hook"; hook "$action"; fi
  fi
fi
exec "$real" "$@"
EOF
chmod +x "$work/bin/curl"

python3 -I -c '
import functools, http.server, sys
h = functools.partial(http.server.SimpleHTTPRequestHandler, directory=sys.argv[1])
h.log_message = lambda *a: None
s = http.server.HTTPServer(("127.0.0.1", 0), h)
open(sys.argv[2], "w").write(str(s.server_address[1]))
s.serve_forever()' "$work/www" "$work/www.port" </dev/null >/dev/null 2>&1 &
pids+=($!)
for _ in $(seq 50); do [[ -s "$work/www.port" ]] && break; sleep 0.1; done
www="http://127.0.0.1:$(cat "$work/www.port")"

# inst PKG [ACTION]: runs the installer on that package, with the hook armed.
inst() {
  rm -f "$work/hook.n"
  if [[ -n "${2:-}" ]]; then
    printf '%s %s\n' "$2" "$(sha256sum <"$work/pkg-$1/ergosphere-test/ergosphere.py" | cut -d' ' -f1)" >"$work/hook"
  fi
  env -u ERGOSPHERE_PORT -u ERGOSPHERE_CONFIG HOME="$home" PATH="$work/bin:$PATH" TMPDIR="$work" \
    ERGOSPHERE_INSTALL_SOURCE_URL="$www/$1.tar.gz" \
    setsid -w bash "$installer" --auto --dir "$dest" --port "$port" </dev/null 2>&1
}
field() { sed -n "s/^  $1: *//p" <<<"$2" | head -n 1; }
pid_now() {
  "$real_curl" --noproxy '*' -s --max-time 3 "http://127.0.0.1:$port/health" |
    python3 -I -c 'import json,sys; print(json.load(sys.stdin)["pid"])' 2>/dev/null
}
track() {
  [[ -n "$1" && " ${pids[*]} " != *" $1 "* ]] && pids+=("$1")
  return 0
}
hooked() { grep -q "^$1 done" "$work/hook.log" 2>/dev/null; }

out=$(inst v1); rc=$?
pid1=$(field pid "$out")
track "$pid1"
if [[ "$rc" -ne 0 || -z "$pid1" || "$(pid_now)" != "$pid1" ]]; then
  bad "fresh install: rc $rc, pid '${pid1:-none}'"
  printf '%s\n' "$out" | tail -n 15
  exit 1
fi

# 1. A session starts after the installer looked: nothing is killed.
out=$(inst v2 launch); rc=$?
track "$(field pid "$out")"
gpid=$(cat "$work/grok.pid" 2>/dev/null)
if ! hooked launch || [[ -z "$gpid" ]]; then
  bad "launch: the hook did not start a session ($(cat "$work/hook.out" 2>/dev/null))"
  printf '%s\n' "$out" | tail -n 15
  exit 1
fi
if [[ "$rc" -eq 0 && "$(pid_now)" == "$pid1" ]] && kill -0 "$gpid" 2>/dev/null &&
    [[ "$out" == *"not restarted (1 grok session open)"* ]]; then
  ok "launch: pid $pid1 kept, its grok $gpid still runs"
else
  bad "launch: grok $(kill -0 "$gpid" 2>/dev/null && echo alive || echo killed), server pid $pid1 -> $(pid_now), rc $rc"
  printf '%s\n' "$out" | tail -n 15
fi

# 2. The only session ends after the installer looked: the server restarts.
# Case 1 left one grok running; if it was killed, start one here.
pid_before=$(pid_now)
track "$pid_before"
if ! kill -0 "$gpid" 2>/dev/null; then
  "$real_curl" --noproxy '*' -s --max-time 5 -H "Origin: http://127.0.0.1:$port" -H 'Content-Type: application/json' \
    --data "{\"approval\":\"ask\",\"cwd\":\"$work\"}" "http://127.0.0.1:$port/launch" >/dev/null
  for _ in $(seq 100); do pgrep -P "$pid_before" >"$work/grok.pid" && break; sleep 0.1; done
fi
out=$(inst v3 end); rc=$?
pid2=$(field pid "$out")
track "$pid2"
if hooked end && [[ "$rc" -eq 0 && -n "$pid2" && "$pid2" != "$pid_before" && "$(pid_now)" == "$pid2" ]]; then
  ok "end: pid $pid_before -> $pid2 after its last session ended"
else
  bad "end: server not restarted (pid $pid_before -> ${pid2:-none}), hook $(hooked end && echo ran || echo 'never ran'), rc $rc"
  printf '%s\n' "$out" | tail -n 15
fi

# 3. A Settings restart (same pid, re-exec) after the installer looked.
# A grok case 2 left (its hook did not run) would keep the server: end it.
rm -f "$work/hook"
if [[ -s "$work/grok.pid" ]] && kill "$(cat "$work/grok.pid")" 2>/dev/null; then
  for _ in $(seq 100); do
    [[ "$("$real_curl" --noproxy '*' -s --max-time 3 "http://127.0.0.1:$port/session-info")" == *'"running": 0'* ]] && break
    sleep 0.1
  done
fi
pid_before=$(pid_now)
track "$pid_before"
out=$(inst v4 restart); rc=$?
pid3=$(field pid "$out")
track "$pid3"
if hooked restart && [[ "$rc" -eq 0 && -n "$pid3" && "$pid3" != "$pid_before" && "$(pid_now)" == "$pid3" ]] &&
    ! kill -0 "$pid_before" 2>/dev/null; then
  ok "restart: pid $pid_before -> $pid3 after a Settings restart"
else
  bad "restart: rc $rc, pid '$pid3' (was $pid_before), hook: $(cat "$work/hook.out" 2>/dev/null)"
  printf '%s\n' "$out" | tail -n 15
fi

exit "$fail"
