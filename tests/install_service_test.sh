#!/usr/bin/env bash
# install.sh --service writes and enables a systemd user unit (Type=notify,
# watchdog, Restart=always) whose main pid serves /health; a rerun changes
# nothing; --no-service removes it. Runs against a private systemd user
# manager with a throwaway HOME, never the real one; SKIP without systemd.
#
# Run: ERGOSPHERE_TEST_PORT=<spare port> bash tests/install_service_test.sh
# No network: the package is served from 127.0.0.1 and grok is a stub that
# only answers --version. The private manager and every server are stopped.
set -u
repo=$(cd "$(dirname "$0")/.." && pwd)
port=${ERGOSPHERE_TEST_PORT:-9551}
manager=/usr/lib/systemd/systemd
mgr_unit="ergo-wd-test-mgr-$$"
if ! command -v systemd-run >/dev/null || [[ ! -x "$manager" ]] ||
  ! systemctl --user show-environment >/dev/null 2>&1; then
  echo "SKIP: no systemd user manager"
  exit 0
fi
# Short path: the manager's sockets live under it (108-byte limit).
work=$(mktemp -d /tmp/ergo-svc.XXXXXX)
home="$work/h" rt="$work/r"
mkdir -p "$home" "$rt" "$work/bin" "$work/www" "$work/pkg/ergosphere-test"
chmod 700 "$rt"
pids=()
fail=0

# shellcheck disable=SC2317  # run by the EXIT trap
cleanup() {
  local p
  systemctl --user stop "$mgr_unit" 2>/dev/null
  systemctl --user reset-failed "$mgr_unit" 2>/dev/null
  for p in ${pids[@]+"${pids[@]}"}; do kill "$p" 2>/dev/null; done
  # Stub grok sessions record their own pid.
  if [[ -f "$work/grok.pids" ]]; then
    while read -r p; do kill "$p" 2>/dev/null; done <"$work/grok.pids"
  fi
  rm -rf -- "$work"
}
trap cleanup EXIT

ok() { echo "ok:   $1"; }
bad() { echo "FAIL: $1"; fail=1; }

# Everything below talks to the private manager only.
sc() {
  env -u DBUS_SESSION_BUS_ADDRESS XDG_RUNTIME_DIR="$rt" HOME="$home" XDG_CONFIG_HOME="$home/.config" \
    systemctl --user "$@"
}

systemd-run --user --quiet --unit="$mgr_unit" -p Delegate=yes -p Type=notify \
  -E HOME="$home" -E XDG_RUNTIME_DIR="$rt" -E XDG_CONFIG_HOME="$home/.config" \
  "$manager" --user --unit=basic.target || { bad "private user manager did not start"; exit 1; }
for _ in $(seq 50); do sc is-system-running >/dev/null 2>&1 && break; sleep 0.1; done
if [[ "$(sc show-environment 2>/dev/null | sed -n 's/^HOME=//p')" != "$home" ]]; then
  bad "systemctl does not reach the private manager"
  exit 1
fi

cp -p "$repo/ergosphere.py" "$repo/LICENSE" "$work/pkg/ergosphere-test/"
cp -rp "$repo/vendor" "$work/pkg/ergosphere-test/"
tar -czf "$work/www/src.tar.gz" -C "$work/pkg" ergosphere-test
# An older release: no SYSTEMD_NOTIFY marker and no READY=1.
mkdir -p "$work/old/ergosphere-old"
cp -rp "$work/pkg/ergosphere-test/." "$work/old/ergosphere-old/"
sed -i -e '/^SYSTEMD_NOTIFY = 1$/d' -e 's/^    sd_notify("READY=1")$/    pass/' "$work/old/ergosphere-old/ergosphere.py"
tar -czf "$work/www/old.tar.gz" -C "$work/old" ergosphere-old
# Answers --version; a launch (--cwd ...) is a long-running stand-in for a
# grok session; anything else (the model list) exits at once.
# shellcheck disable=SC2016  # $1 and $$ belong to the stub
printf '#!/bin/sh\n[ "$1" = --version ] && { echo "grok 0.0.0-test"; exit 0; }\n[ "$1" = --cwd ] || exit 0\necho $$ >>%s/grok.pids\nexec sleep 300\n' "$work" >"$work/bin/grok"
chmod +x "$work/bin/grok"
python3 -I -c '
import functools, http.server, sys
h = functools.partial(http.server.SimpleHTTPRequestHandler, directory=sys.argv[1])
h.log_message = lambda *a: None
s = http.server.HTTPServer(("127.0.0.1", 0), h)
open(sys.argv[2], "w").write(str(s.server_address[1]))
s.serve_forever()' "$work/www" "$work/www.port" </dev/null >/dev/null 2>&1 &
pids+=($!)
for _ in $(seq 50); do [[ -s "$work/www.port" ]] && break; sleep 0.1; done
url="http://127.0.0.1:$(cat "$work/www.port")/src.tar.gz"

run_install() {
  env -u ERGOSPHERE_PORT -u ERGOSPHERE_CONFIG -u DBUS_SESSION_BUS_ADDRESS HOME="$home" \
    XDG_RUNTIME_DIR="$rt" XDG_CONFIG_HOME="$home/.config" PATH="$work/bin:$PATH" TMPDIR="$work" \
    ERGOSPHERE_INSTALL_SOURCE_URL="${src_url:-$url}" timeout 60 bash "$repo/install.sh" --auto --dir "$home/app" --port "$port" "$@" </dev/null 2>&1
}
field() { sed -n "s/^  $1: *//p" <<<"$2" | head -n 1; }
health_pid() {
  curl --noproxy '*' -s --max-time 3 "http://127.0.0.1:$port/health" |
    python3 -I -c 'import json,sys; print(json.load(sys.stdin).get("pid", ""))' 2>/dev/null
}
unit="$home/.config/systemd/user/ergosphere.service"

# 1. --service: unit written, enabled, active (READY=1 seen), main pid serves /health.
out=$(run_install --service)
if ! grep -q '^Ergosphere is running' <<<"$out"; then
  bad "install --service did not finish"
  printf '%s\n' "$out" | tail -n 15
  exit 1
fi
for line in Type=notify WatchdogSec=60 Restart=always RestartSec=5 WantedBy=default.target; do
  if grep -qxF "$line" "$unit" 2>/dev/null; then ok "unit has $line"; else bad "unit lacks $line"; fi
done
if [[ "$(sc is-enabled ergosphere.service 2>/dev/null)" == enabled ]]; then ok "unit enabled"; else bad "unit not enabled"; fi
main=$(sc show -p MainPID --value ergosphere.service)
if [[ "$(sc show -p ActiveState --value ergosphere.service)" == active && "$main" != 0 &&
      "$(health_pid)" == "$main" && "$(field pid "$out")" == "$main" ]]; then
  ok "active, /health pid = MainPID = summary pid ($main)"
else
  bad "unit state $(sc show -p ActiveState --value ergosphere.service), MainPID $main, /health pid $(health_pid), summary pid $(field pid "$out")"
fi
if grep -q 'loginctl enable-linger' <<<"$out" || [[ "$(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null)" == yes ]]; then
  ok "summary names loginctl enable-linger (or linger is on)"
else
  bad "summary does not mention loginctl enable-linger"
fi

# 2. Rerun without the flag keeps the unit and changes nothing.
sum=$(sha256sum <"$unit")
out=$(run_install)
if [[ "$(field changed "$out")" == nothing* && "$(sha256sum <"$unit")" == "$sum" &&
      "$(sc show -p MainPID --value ergosphere.service)" == "$main" ]]; then
  ok "rerun changed nothing (same unit, same pid)"
else
  bad "rerun changed '$(field changed "$out")'"
fi

# 3. --check --no-service changes nothing.
run_install --check --no-service >/dev/null
if [[ -f "$unit" && "$(sc show -p MainPID --value ergosphere.service)" == "$main" ]]; then ok "--check changed nothing"; else bad "--check changed the unit"; fi

# 4. --no-service removes the unit; the server runs without systemd.
out=$(run_install --no-service)
pid=$(field pid "$out")
if [[ -n "$pid" ]]; then pids+=("$pid"); fi
if [[ ! -e "$unit" && "$(sc is-enabled ergosphere.service 2>/dev/null)" != enabled &&
      -n "$pid" && "$(health_pid)" == "$pid" && "$pid" != "$main" ]]; then
  ok "--no-service removed the unit; plain server pid $pid serves /health"
else
  bad "--no-service: unit present=$([[ -e "$unit" ]] && echo yes || echo no), pid '$pid', /health pid $(health_pid)"
  printf '%s\n' "$out" | tail -n 8
fi

# 5. A rerun without the flag does not bring the unit back.
out=$(run_install)
if [[ ! -e "$unit" && "$(field changed "$out")" == nothing* ]]; then ok "rerun without --service keeps it removed"; else bad "rerun recreated the unit or changed '$(field changed "$out")'"; fi

# 6. --service again: the unit takes over from the plain server.
run_install --service >/dev/null
main=$(sc show -p MainPID --value ergosphere.service)
if [[ -f "$unit" && "$main" != 0 && "$main" != "$pid" && "$(health_pid)" == "$main" ]] && ! kill -0 "$pid" 2>/dev/null; then
  ok "--service took over from plain pid $pid (unit pid $main)"
else
  bad "takeover: unit pid $main, plain pid $pid, /health pid $(health_pid)"
fi

# 7. --service with an older release (no READY=1): Type=simple, no watchdog, still serving.
t0=$SECONDS
out=$(src_url="${url%/*}/old.tar.gz" run_install --service)
took=$((SECONDS - t0))
main=$(sc show -p MainPID --value ergosphere.service)
if grep -qxF Type=simple "$unit" && ! grep -q '^WatchdogSec=' "$unit" && grep -qxF Restart=always "$unit" &&
   [[ "$(sc show -p ActiveState --value ergosphere.service)" == active && "$main" != 0 && "$(health_pid)" == "$main" ]] &&
   grep -q 'no watchdog support' <<<"$out"; then
  ok "older release: Type=simple, no WatchdogSec, pid $main serves /health, summary says so (${took}s)"
else
  bad "older release (${took}s): Type=$(sed -n 's/^Type=//p' "$unit"), state $(sc show -p ActiveState --value ergosphere.service), MainPID $main, /health pid $(health_pid)"
  printf '%s\n' "$out" | tail -n 6
fi

# 8. Deferred takeover: a plain server with a grok session open keeps running;
# the summary's stop line names that pid, not the unit.
run_install --no-service >/dev/null
pid=$(health_pid)
if [[ -n "$pid" ]]; then pids+=("$pid"); fi
curl --noproxy '*' -s --max-time 5 -H 'Content-Type: application/json' -H "Origin: http://127.0.0.1:$port" \
  -d '{"cwd": "/tmp", "approval": "ask"}' "http://127.0.0.1:$port/launch" >/dev/null
out=$(run_install --service)
stop=$(field stop "$out")
if [[ "$(health_pid)" == "$pid" && "$stop" == "kill $pid "* ]] && grep -q 'keeps running' <<<"$out"; then
  ok "deferred takeover: plain pid $pid kept, stop line is 'kill $pid'"
else
  bad "deferred takeover: /health pid $(health_pid) (plain $pid), stop line '$stop'"
fi

exit "$fail"
