#!/usr/bin/env bash
# Copyright (C) 2026 Julius Perkins
# Licensed under the GNU Affero General Public License v3.0 only.
# SPDX-License-Identifier: AGPL-3.0-only
#
# Ergosphere installer. The whole file is one { } group that ends on the last
# byte, so bash reads all of it before running anything and a truncated
# download is a syntax error that runs nothing. When piped, stdin is this
# script: prompts read /dev/tty. Every run observes the real state (files by
# sha256, config bytes, /health), changes only what differs, then reads the
# result back.

{
usage() {
  cat <<EOF
Ergosphere installer (installs the latest release unless --version is given)
  curl -fsSL ${SCRIPT_URL} | bash
  ... | bash -s -- [options]
With a terminal it asks: Automatic (recommended) or Manual? [A/m]
No terminal means automatic. Prerequisites are checked first and never installed.
  --auto       automatic, no questions
  --manual     ask install dir, port, bind, optional agent, start on boot (on /dev/tty)
  --dir DIR    install dir (default: \$HOME/.local/share/ergosphere)
  --port N     port (default: ERGOSPHERE_PORT, else ergosphere.json, else 9241)
  --bind ADDR  IPv4 address or localhost (default: ergosphere.json, else 127.0.0.1)
Settings live in the file named by ERGOSPHERE_CONFIG when it is set, else in
ergosphere.json next to ergosphere.py. A rerun keeps them;
--bind and --port change only those two keys.
  --service    run it as a systemd user unit (ergosphere.service): starts at
               login (at boot with loginctl enable-linger), restarts if it
               crashes or hangs. A rerun without this flag keeps the unit.
  --no-service remove that unit and run it without systemd
  --yes        approve a non-loopback bind without asking
  --version vA.B.C  install this release instead of the latest (pin or roll back)
  --check      run the checks only; change nothing
  --help       this help
EOF
}

fail() {
  printf 'failed at %s: %s\n' "$1" "$2" >&2
  exit 1
}

cleanup() {
  local status=$?
  trap - EXIT
  # A server this run started but never verified is stopped, not left behind.
  if [[ -n "${STARTED_PID:-}" && "${VERIFIED:-0}" -eq 0 ]]; then
    kill "$STARTED_PID" 2>/dev/null || true
  fi
  if [[ "${UNIT_STARTED:-0}" -eq 1 && "${VERIFIED:-0}" -eq 0 ]]; then
    systemctl --user stop ergosphere.service 2>/dev/null || true
  fi
  # A failed run puts the unit file back as it found it (or removes a new one);
  # enabling happens only after a good start. The code is NOT rolled back.
  if [[ "$status" -ne 0 && "${UNIT_WRITTEN:-0}" -eq 1 ]]; then
    if [[ "${UNIT_HAD_FILE:-0}" -eq 1 && -f "${STAGE:-}/unit.prev" ]]; then
      cp -p -- "$STAGE/unit.prev" "$UNIT_FILE" 2>/dev/null || true
      printf 'service: put the unit file %s back as it was before this run\n' "$UNIT_FILE" >&2
    else
      rm -f -- "$UNIT_FILE"
      printf 'service: removed the unit file %s that this run wrote; nothing was enabled\n' "$UNIT_FILE" >&2
    fi
    systemctl --user daemon-reload 2>/dev/null || true
    if [[ "${UNIT_WAS_ACTIVE:-0}" -eq 1 ]]; then
      systemctl --user start ergosphere.service 2>/dev/null || true
    fi
    # Only an existing unit is left behind to run the new code, and to need a reset.
    if [[ "${UNIT_HAD_FILE:-0}" -eq 1 && "${CODE_UPDATED:-0}" -gt 0 ]]; then
      printf 'service: the code in %s was NOT put back: it is the new release (%s files updated), so the unit may still fail to start\n' "$DEST" "$CODE_UPDATED" >&2
    fi
  fi
  if [[ "$status" -ne 0 && "${UNIT_STARTED:-0}" -eq 1 && -f "${UNIT_FILE:-}" ]]; then
    printf 'service: after fixing the cause, run: systemctl --user reset-failed ergosphere   (it stops retrying after 5 failed starts), then rerun this installer\n' >&2
  fi
  if [[ -n "${STAGE:-}" ]]; then
    rm -rf -- "$STAGE"
  fi
  exit "$status"
}

row() {
  printf '  %-8s %-10s %s\n' "$1" "$2" "$3"
  if [[ "$1" != ok && "$1" != warn ]]; then
    BLOCKERS+=("$2")
  fi
}

sha_of() {
  sha256sum 2>/dev/null <"$1" | cut -d' ' -f1
}

tty_ok() {
  ( : </dev/tty >/dev/tty ) 2>/dev/null
}

ask() {
  tty_ok || fail check "manual mode needs a terminal (/dev/tty)"
  printf '%s' "$1" >/dev/tty
  IFS= read -r REPLY </dev/tty || fail check "could not read an answer"
  REPLY=${REPLY%$'\r'}
}

# same_addr BIND:PORT BIND:PORT. localhost is 127.0.0.1, as the server binds it.
same_addr() {
  local a=$1 b=$2
  if [[ "$a" == localhost:* ]]; then a=127.0.0.1:${a#*:}; fi
  if [[ "$b" == localhost:* ]]; then b=127.0.0.1:${b#*:}; fi
  [[ "$a" == "$b" ]]
}

is_loopback() {
  [[ "$1" == localhost || "$1" =~ ^127\. ]]
}

set_port() {
  if ! [[ "$1" =~ ^[0-9]{1,5}$ ]] || (( 10#$1 < 1 || 10#$1 > 65535 )); then
    fail check "port must be 1-65535: $1"
  fi
  PORT=$((10#$1))
}

set_bind() {
  local o
  if [[ "${1,,}" == localhost ]]; then
    BIND=localhost
    return 0
  fi
  [[ "$1" =~ ^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})$ ]] ||
    fail check "bind must be an IPv4 address or localhost: $1"
  for o in "${BASH_REMATCH[@]:1}"; do
    (( 10#$o <= 255 )) || fail check "bind must be an IPv4 address or localhost: $1"
  done
  BIND=$1
}

set_dir() {
  local d=$1
  if [[ "$d" == \~ || "$d" == \~/* ]]; then
    d="${HOME:-}${d:1}"
  fi
  [[ "$d" == /?* ]] || fail check "install dir must be an absolute path: $1"
  DEST=${d%/}
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --help) usage; exit 0 ;;
      --check) WANT_CHECK=1 ;;
      --auto) MODE=automatic ;;
      --manual) MODE=manual ;;
      --yes) ASSUME_YES=1 ;;
      --service) SERVICE=1 ;;
      --no-service) SERVICE=0 ;;
      --dir|--port|--bind|--version)
        [[ $# -ge 2 ]] || fail check "missing value for $1"
        case "$1" in
          --version)
            [[ "$2" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail check "--version needs a release tag like vA.B.C (digits), not $2"
            RELEASE=$2 PINNED=1 ;;
          --dir) set_dir "$2" ;;
          --port) set_port "$2"; ARG_PORT=$PORT ;;
          --bind) set_bind "$2"; ARG_BIND=$BIND ;;
        esac
        shift
        ;;
      *) fail check "unknown option: $1 (see --help)" ;;
    esac
    shift
  done
}

# Host prerequisites. Runs before any question, lists every item, and stops on
# anything missing without changing anything.
check_host() {
  local tool path ver rc t=()
  printf 'Checking prerequisites:\n'
  if [[ "${OSTYPE:-}" == linux* && -r /proc/self/stat && -d /proc/self/fd ]]; then
    row ok linux "${OSTYPE} with /proc"
  else
    row missing linux "need Linux with /proc (found ${OSTYPE:-unknown})"
  fi
  PYTHON=$(command -v python3 || true)
  if [[ -z "$PYTHON" ]]; then
    row missing python3 "install Python 3"
  elif ver=$("$PYTHON" -I -c 'import sys, pty, termios, fcntl; assert sys.version_info[0] == 3; print(sys.version.split()[0])' 2>/dev/null </dev/null); then
    row ok python3 "$PYTHON $ver (pty termios fcntl)"
  else
    row invalid python3 "$PYTHON is not a working Python 3 with pty, termios, fcntl"
  fi
  for tool in curl tar gzip nohup sha256sum; do
    path=$(command -v "$tool" || true)
    if [[ -n "$path" ]]; then
      row ok "$tool" "$path"
    else
      row missing "$tool" "install $tool"
    fi
  done
  CURL=$(command -v curl || true)
  GROK=$(command -v grok || true)
  if [[ -z "$GROK" ]]; then
    row missing grok "install grok and sign in"
  else
    # Keep the exit status: a broken grok that prints an error must block.
    if command -v timeout >/dev/null; then t=(timeout 15s); fi
    rc=0
    ver=$("${t[@]}" "$GROK" --version 2>&1 </dev/null) || rc=$?
    ver=${ver%%$'\n'*}
    if [[ "$rc" -eq 0 && -n "$ver" ]]; then
      row ok grok "$GROK $ver"
    else
      row invalid grok "$GROK --version exit $rc: ${ver:-no output}"
    fi
  fi
  printf '  note     grok sign-in is not checked; grok must already be signed in\n'
  if [[ ${#BLOCKERS[@]} -gt 0 ]]; then
    fail check "missing or broken: ${BLOCKERS[*]}. Install them and re-run; this installer does not install prerequisites"
  fi
  if [[ -n "${ERGOSPHERE_TOKEN:-}" ]]; then
    fail check "ERGOSPHERE_TOKEN is set. Token mode is for reverse proxies; unset ERGOSPHERE_TOKEN to use this installer"
  fi
}

choose_mode() {
  if [[ -n "$MODE" ]]; then
    return 0
  fi
  MODE=automatic
  tty_ok || return 0
  printf 'Automatic (recommended) or Manual? [A/m] ' >/dev/tty
  IFS= read -r REPLY </dev/tty || fail check "could not read the mode answer"
  REPLY=${REPLY%$'\r'}
  case "${REPLY,,}" in
    ""|a|auto|automatic) ;;
    m|manual) MODE=manual ;;
    *) fail check "answer A or m" ;;
  esac
}

ask_dir() {
  ask "Install dir [${DEST}]: "
  if [[ -n "$REPLY" ]]; then set_dir "$REPLY"; fi
}

# Asked after the config is read, so Enter keeps the saved values.
ask_manual() {
  ask "Port [${PORT}]: "
  if [[ -n "$REPLY" ]]; then set_port "$REPLY"; ARG_PORT=$PORT; fi
  ask "Bind [${BIND}] (anything but 127.0.0.1 lets anyone who reaches the port run grok as this user): "
  if [[ -n "$REPLY" ]]; then set_bind "$REPLY"; ARG_BIND=$BIND; fi
  ask "Agent name for ergosphere.json (optional, Enter to skip): "
  AGENT=$REPLY
  if [[ -n "$AGENT" ]]; then
    while :; do
      ask "Working directory for ${AGENT}: "
      AGENT_DIR=$REPLY
      if [[ "$AGENT_DIR" == /* && -d "$AGENT_DIR" ]]; then break; fi
      printf 'not an existing absolute directory: %s\n' "$AGENT_DIR" >/dev/tty
    done
  fi
  read_unit
  if [[ -z "$SERVICE" && ( "$UNIT_STATE" == absent || "$UNIT_STATE" == ours ) ]]; then
    local hint="y/N"
    if [[ "$UNIT_STATE" == ours ]]; then hint="Y/n"; fi
    ask "Start on login/boot and restart if it crashes or hangs (systemd user unit)? [$hint] "
    case "${REPLY,,}" in
      "") if [[ "$UNIT_STATE" == ours ]]; then SERVICE=1; else SERVICE=0; fi ;;
      y|yes) SERVICE=1 ;;
      n|no) SERVICE=0 ;;
      *) fail check "answer y or n" ;;
    esac
  fi
}

# systemd user unit (--service). One per user, ergosphere.service, owned by
# the install dir in its marker line; a unit without our marker is never touched.
user_systemd() {
  command -v systemctl >/dev/null && systemctl --user show-environment >/dev/null 2>&1
}

# A unit line holds these characters without quoting or %-specifiers.
unit_safe() {
  [[ "$1" =~ ^[A-Za-z0-9/._:+@-]+$ ]]
}

# Sets UNIT_FILE and UNIT_STATE: none (no systemd user manager), absent,
# ours (marker names this install dir) or foreign (another install's file, or an
# ergosphere.service the manager loads from another search path: UNIT_FOREIGN).
read_unit() {
  local frag
  UNIT_FILE="${XDG_CONFIG_HOME:-${HOME:-}/.config}/systemd/user/ergosphere.service"
  UNIT_FOREIGN="" UNIT_DROPINS=""
  if ! user_systemd; then
    UNIT_STATE=none
    return 0
  fi
  # The manager may load ergosphere.service from another search path
  # (/etc/systemd/user, $XDG_DATA_HOME, $XDG_CONFIG_DIRS, ...). That one is
  # someone else's: never shadow, start, stop or enable it.
  frag=$(systemctl --user show -p FragmentPath --value ergosphere.service 2>/dev/null) || frag=""
  UNIT_DROPINS=$(systemctl --user show -p DropInPaths --value ergosphere.service 2>/dev/null) || UNIT_DROPINS=""
  if [[ -n "$frag" && "$frag" != "$UNIT_FILE" ]]; then
    UNIT_STATE=foreign UNIT_FOREIGN=$frag
  elif [[ ! -e "$UNIT_FILE" ]]; then
    UNIT_STATE=absent
  elif grep -qxF "# ergosphere-install-dir: $DEST" "$UNIT_FILE" 2>/dev/null; then
    UNIT_STATE=ours
  else
    UNIT_STATE=foreign
  fi
}

# The unit's main pid, or nothing when it is not running.
unit_main_pid() {
  local pid
  pid=$(systemctl --user show -p MainPID --value ergosphere.service 2>/dev/null) || pid=""
  if [[ "$pid" =~ ^[1-9][0-9]*$ ]]; then printf '%s\n' "$pid"; fi
}

# Decides WANT_UNIT: --service, --no-service, else keep what is installed.
check_unit() {
  local main kids
  read_unit
  WANT_UNIT=0
  case "$SERVICE:$UNIT_STATE" in
    1:none) row missing service "--service needs a systemd user manager (systemctl --user)" ;;
    [01]:foreign) row invalid service "${UNIT_FOREIGN:-$UNIT_FILE} is an ergosphere.service from another install; it was not touched" ;;
    1:*|:ours) WANT_UNIT=1 ;;
    0:ours)
      main=$(unit_main_pid)
      kids=0
      if [[ -n "$main" ]]; then kids=$(child_count "$main"); fi
      if [[ "$kids" -gt 0 ]]; then
        row invalid service "ergosphere.service (pid $main) has $kids grok session(s) open; rerun --no-service when they are done"
      else
        row ok service "$UNIT_FILE will be stopped, disabled and removed"
      fi
      ;;
    *:foreign) row warn service "${UNIT_FOREIGN:-$UNIT_FILE} is an ergosphere.service from another install; left alone" ;;
    *:absent) row ok service "no systemd unit (--service starts it at login and restarts it if it crashes or hangs)" ;;
  esac
  if [[ "$WANT_UNIT" -eq 1 && -n "$UNIT_DROPINS" ]]; then
    row warn service "drop-ins also change ergosphere.service: $UNIT_DROPINS"
  fi
  if [[ "$WANT_UNIT" -eq 1 ]]; then
    if ! unit_safe "$DEST" || ! unit_safe "$PYTHON" || ! unit_safe "$CFG_FILE" || ! unit_safe "${GROK%/*}"; then
      row invalid service "a systemd unit needs install dir, python, grok and config paths made of letters, digits and / . _ : + @ - (no spaces)"
    elif [[ "$UNIT_STATE" == ours ]]; then
      row ok service "$UNIT_FILE present (starts at login, restarts on crash or hang)"
    else
      row ok service "$UNIT_FILE will be written and enabled (starts at login, restarts on crash or hang)"
    fi
  fi
}

unit_text() {
  local p path="" parts=()
  # The server finds grok on PATH, as with the plain start: grok's dir first,
  # then the PATH entries a unit line can hold.
  IFS=: read -r -a parts <<<"${GROK%/*}:${PATH:-/usr/bin:/bin}"
  for p in "${parts[@]}"; do
    if [[ "$p" == /* ]] && unit_safe "$p" && [[ ":$path:" != *":$p:"* ]]; then path+="${path:+:}$p"; fi
  done
  printf '%s\n' "# Written by the Ergosphere installer; a rerun updates it, --no-service removes it." \
    "# ergosphere-install-dir: $DEST" \
    "[Unit]" \
    "Description=Ergosphere web portal for grok sessions" \
    "# Stop retrying when it cannot start (another server holds the port)." \
    "StartLimitIntervalSec=60" \
    "StartLimitBurst=5" \
    "" \
    "[Service]" \
    "Type=$UNIT_TYPE" \
    "WorkingDirectory=$DEST" \
    "Environment=PATH=$path"
  if [[ -n "${ERGOSPHERE_CONFIG:-}" ]]; then printf 'Environment=ERGOSPHERE_CONFIG=%s\n' "$CFG_FILE"; fi
  printf '%s\n' "ExecStart=$PYTHON $DEST/ergosphere.py" \
    "StandardOutput=append:$DEST/ergosphere.log" \
    "StandardError=append:$DEST/ergosphere.log"
  if [[ "$UNIT_TYPE" == notify ]]; then printf 'WatchdogSec=60\n'; fi
  printf '%s\n' "Restart=always" \
    "RestartSec=5" \
    "" \
    "[Install]" \
    "WantedBy=default.target"
}

# Writes the unit when it differs and reads it back (enable_unit enables it later).
install_unit() {
  local want
  [[ "$WANT_UNIT" -eq 1 ]] || return 0
  # Only code with the SYSTEMD_NOTIFY marker sends READY=1; older releases
  # (--version rollback) would never count as started under Type=notify.
  UNIT_TYPE=simple
  if grep -qE '^SYSTEMD_NOTIFY = 1( |$)' "$DEST/ergosphere.py"; then UNIT_TYPE=notify; fi
  want=$(unit_text)
  if [[ ! -f "$UNIT_FILE" || "$(cat -- "$UNIT_FILE")" != "$want" ]]; then
    # Kept so a failed run can put the unit back (cleanup).
    UNIT_HAD_FILE=0 UNIT_WAS_ACTIVE=0
    if [[ -f "$UNIT_FILE" ]]; then
      cp -p -- "$UNIT_FILE" "$STAGE/unit.prev" || fail service "could not keep a copy of $UNIT_FILE"
      UNIT_HAD_FILE=1
      if systemctl --user is-active --quiet ergosphere.service 2>/dev/null; then UNIT_WAS_ACTIVE=1; fi
    fi
    UNIT_WRITTEN=1
    { mkdir -p -- "${UNIT_FILE%/*}" && printf '%s\n' "$want" >"$UNIT_FILE.part" &&
      mv -f -- "$UNIT_FILE.part" "$UNIT_FILE"; } || fail service "could not write $UNIT_FILE"
    systemctl --user daemon-reload || fail service "systemctl --user daemon-reload failed"
    UNIT_CHANGED=1
    printf 'service: wrote %s\n' "$UNIT_FILE"
    CHANGED+=("ergosphere.service")
  fi
  [[ "$(cat -- "$UNIT_FILE")" == "$want" ]] || fail service "read-back: $UNIT_FILE does not hold the intended unit"
}

# Enables the unit once start_server succeeded (or kept a busy server for it),
# so a failed start never leaves an enabled unit that fails at every login.
enable_unit() {
  [[ "$WANT_UNIT" -eq 1 ]] || return 0
  if ! systemctl --user is-enabled --quiet ergosphere.service 2>/dev/null; then
    systemctl --user enable --quiet ergosphere.service || fail service "could not enable ergosphere.service"
    CHANGED+=("enabled ergosphere.service")
  fi
  systemctl --user is-enabled --quiet ergosphere.service 2>/dev/null || fail service "read-back: ergosphere.service is not enabled"
  printf 'service: ergosphere.service is enabled (read back)\n'
}

# --no-service: stop (check_unit saw no grok sessions), disable, remove.
remove_unit() {
  local main
  [[ "$UNIT_STATE" == ours && "$WANT_UNIT" -eq 0 ]] || return 0
  main=$(unit_main_pid)
  if [[ -n "$main" ]]; then
    [[ "$(child_count "$main")" -eq 0 ]] || fail service "ergosphere.service pid $main has grok sessions open; it was left running"
    systemctl --user stop ergosphere.service || fail service "could not stop ergosphere.service"
    CHANGED+=("stopped ergosphere.service")
  fi
  systemctl --user disable --quiet ergosphere.service 2>/dev/null || true
  rm -f -- "$UNIT_FILE"
  systemctl --user daemon-reload || fail service "systemctl --user daemon-reload failed"
  if [[ -e "$UNIT_FILE" ]] || systemctl --user is-enabled --quiet ergosphere.service 2>/dev/null; then
    fail service "read-back: ergosphere.service is still installed"
  fi
  printf 'service: removed %s (read back)\n' "$UNIT_FILE"
  CHANGED+=("removed ergosphere.service")
}

# Starts or restarts the unit (Type=notify waits for READY=1) and reads back
# that its main pid answers /health.
start_unit() {
  local verb=$1 main deadline
  UNIT_STARTED=1
  systemctl --user "$verb" ergosphere.service ||
    fail start "systemctl --user $verb ergosphere.service failed; see journalctl --user -u ergosphere.service and $DEST/ergosphere.log"
  deadline=$((SECONDS + 10))
  while (( SECONDS < deadline )); do
    main=$(unit_main_pid)
    probe "$BIND" "$PORT"
    if [[ "$PROBE" == match && -n "$main" && "$PROBE_PID" == "$main" ]]; then
      VERIFIED=1 PID=$main
      printf 'start: ergosphere.service %sed, pid %s on %s:%s (read back from /health and systemctl)\n' "$verb" "$PID" "$BIND" "$PORT"
      CHANGED+=("server ${verb}ed by ergosphere.service")
      return 0
    fi
    sleep 0.2
  done
  fail start "ergosphere.service did not answer /health as its main pid on ${BIND}:${PORT}"
}

# probe BIND PORT. Sets PROBE to match (this product at that bind/port, from
# its own /health), moved (this product on that port but another bind, as
# after a bind change that left a busy server running), down (nothing
# listening) or other, PROBE_PID to the serving pid and PROBE_AT to where it
# listens.
probe() {
  local host=$1 out rc=0 info
  if [[ "$host" == 0.0.0.0 ]]; then host=127.0.0.1; fi
  PROBE=other PROBE_PID="" PROBE_DIGEST="" PROBE_AT="$1:$2"
  out=$("$CURL" --noproxy '*' -s --max-time 3 "http://${host}:${2}/health") || rc=$?
  if [[ "$rc" -eq 7 ]]; then
    PROBE=down
  elif [[ "$rc" -eq 0 ]] && info=$(printf '%s' "$out" | "$PYTHON" -I -c '
import json, re, sys
d = json.load(sys.stdin)
norm = lambda b: "127.0.0.1" if b == "localhost" else b
ok = (isinstance(d, dict) and d.get("ok") is True and d.get("product") == "ergosphere"
      and type(d.get("port")) is int and d["port"] == int(sys.argv[2])
      and d.get("tls") is False and type(d.get("bind")) is str
      and re.fullmatch("[0-9A-Za-z.:]+", d["bind"]) and type(d.get("pid")) is int and d["pid"] > 0)
if not ok: sys.exit(1)
digest = d.get("code_sha256")
print("match" if norm(d["bind"]) == norm(sys.argv[1]) else "moved", d["bind"], d["pid"],
      digest if isinstance(digest, str) and re.fullmatch("[0-9a-f]{64}", digest) else "")
' "$1" "$2" 2>/dev/null); then
    read -r PROBE info PROBE_PID PROBE_DIGEST <<<"$info"
    if [[ "$PROBE" == moved ]]; then PROBE_AT="$info:$2"; fi
  else
    PROBE_PID=
  fi
}

# Prints "bind port pid" for a running server from this install, found by
# its script path in /proc and its listening IPv4 socket, or nothing.
find_own() {
  "$PYTHON" -I -c '
import os, socket, sys
want = os.path.realpath(sys.argv[1])
for pid in sorted((p for p in os.listdir("/proc") if p.isdigit()), key=int):
    try:
        with open("/proc/%s/cmdline" % pid, "rb") as f:
            argv = [a.decode("utf-8", "replace") for a in f.read().split(b"\0") if a]
        cwd = os.readlink("/proc/%s/cwd" % pid)
    except OSError:
        continue
    if not argv or not os.path.basename(argv[0]).startswith("python") or "--check" in argv:
        continue
    script = next((a for a in argv[1:] if a.endswith("ergosphere.py")), None)
    if not script or os.path.realpath(os.path.join(cwd, script)) != want:
        continue
    inodes = set()
    try:
        for fd in os.listdir("/proc/%s/fd" % pid):
            link = os.readlink("/proc/%s/fd/%s" % (pid, fd))
            if link.startswith("socket:["):
                inodes.add(link[8:-1])
        with open("/proc/%s/net/tcp" % pid) as f:
            rows = f.read().splitlines()[1:]
    except OSError:
        continue
    for row in rows:
        cols = row.split()
        if len(cols) > 9 and cols[3] == "0A" and cols[9] in inodes:
            addr, port = cols[1].split(":")
            print(socket.inet_ntoa(bytes.fromhex(addr)[::-1]), int(port, 16), pid)
            sys.exit(0)' "$DEST/ergosphere.py" 2>/dev/null </dev/null || true
}

# When nothing answers on the configured address, a server from this install
# may still run elsewhere: the port changed (here or in Settings) while grok
# sessions kept it running. Sets PROBE=moved and PROBE_AT when one answers.
probe_own() {
  local info b p pid
  info=$(find_own)
  [[ -n "$info" ]] || return 0
  read -r b p pid <<<"$info"
  probe "$b" "$p"
  if [[ "$PROBE" == match || "$PROBE" == moved ]] && [[ "$PROBE_PID" == "$pid" ]]; then
    PROBE=moved PROBE_AT="$b:$p"
    return 0
  fi
  fail check "Ergosphere pid $pid from $DEST listens on $b:$p but its /health did not answer as expected; stop it (kill $pid) and rerun"
}

# A matching /health may come from another install. Compare the running
# script path (resolved against the server's cwd) with this install's.
same_install() {
  local arg script="" cwd
  while IFS= read -r -d '' arg; do
    if [[ "$arg" == *ergosphere.py ]]; then script=$arg; break; fi
  done <"/proc/$PROBE_PID/cmdline"
  cwd=$(readlink "/proc/$PROBE_PID/cwd") || cwd=/
  if [[ "$script" != /* ]]; then script="$cwd/$script"; fi
  script=$(realpath -m -- "$script")
  if [[ "$script" != "$(realpath -m -- "$DEST/ergosphere.py")" ]]; then
    fail check "$PROBE_AT is used by an Ergosphere installed in ${script%/ergosphere.py}; it was not touched"
  fi
}

# Compare loaded runtime bytes, so chmod/touch and documentation edits do not
# restart a server. A legacy server without a fingerprint needs one upgrade.
server_stale() {
  local digest
  [[ -n "$PROBE_DIGEST" ]] || return 0
  digest=$("$PYTHON" -I -c '
import runpy, sys
print(runpy.run_path(sys.argv[1])["runtime_fingerprint"]())
' "$DEST/ergosphere.py" </dev/null) || fail install "could not fingerprint installed runtime"
  [[ "$PROBE_DIGEST" != "$digest" ]]
}

# Number of processes whose parent is $1 (the server's grok sessions).
child_count() {
  local f line ppid n=0
  for f in /proc/[0-9]*/stat; do
    IFS= read -r line 2>/dev/null <"$f" || continue
    read -r _ ppid _ <<<"${line##*) }"
    if [[ "$ppid" == "$1" ]]; then n=$((n + 1)); fi
  done
  printf '%s\n' "$n"
}

# cfg_py read AGENT AGENT_DIR | write BIND PORT SET_BIND SET_PORT AGENT AGENT_DIR
# ergosphere.json is an object with optional bind, port, agents (name to
# absolute path) and last. If it is missing, agents come from a legacy
# cwd-defaults.json, which is never changed. read prints tab-separated
# source/bind/port/agents/agent lines; write changes only what differs, writes
# atomically and reads back. Unknown keys (and last) are kept as they are.
cfg_py() {
  "$PYTHON" -I -c '
import json, os, sys
mode, cfg, legacy = sys.argv[1:4]
def bad(msg):
    print(msg, file=sys.stderr)
    sys.exit(1)
def load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as exc:
        bad("%s is not readable JSON (%s); left unchanged" % (path, exc))
def agents_ok(a):
    return isinstance(a, dict) and all(isinstance(k, str) and k and isinstance(v, str)
                                       and os.path.isabs(v) for k, v in a.items())
if os.path.exists(cfg):
    source, d = "json", load(cfg)
    if not isinstance(d, dict):
        bad(cfg + " is not a JSON object; left unchanged")
    if "bind" in d and not isinstance(d["bind"], str):
        bad(cfg + ": bind must be a string; left unchanged")
    if "port" in d and (type(d["port"]) is not int or not 1 <= d["port"] <= 65535):
        bad(cfg + ": port must be an integer 1-65535; left unchanged")
    if "agents" in d and not agents_ok(d["agents"]):
        bad(cfg + ": agents must map names to absolute paths; left unchanged")
    if "last" in d and not isinstance(d["last"], dict):
        bad(cfg + ": last must be an object; left unchanged")
elif os.path.exists(legacy):
    source, d = "legacy", {"agents": load(legacy)}
    if not agents_ok(d["agents"]):
        bad(legacy + " is not a JSON object of agent to absolute path; left unchanged")
else:
    source, d = "new", {"agents": {}}
agents = d.get("agents", {})
if mode == "read":
    agent, adir = sys.argv[4:6]
    state = ("none" if not agent else "new" if agent not in agents
             else "same" if agents[agent] == adir else "differs")
    print("source\t" + source)
    print("bind\t%s" % d.get("bind", ""))
    print("port\t%s" % d.get("port", ""))
    print("agents\t" + (" ".join("%s=%s" % kv for kv in agents.items()) or "(none)"))
    print("agent\t" + state)
    sys.exit(0)
bind, port, set_bind, set_port, agent, adir = sys.argv[4:10]
if source == "json":
    before = json.loads(json.dumps(d))
    if set_bind == "1":
        d["bind"] = bind
    if set_port == "1":
        d["port"] = int(port)
else:
    before = None
    d = {"bind": bind, "port": int(port), "agents": dict(agents)}
if agent and agent not in d.get("agents", {}):
    d.setdefault("agents", {})[agent] = adir
if d == before:
    print("kept")
    sys.exit(0)
# Named like the server temp files, which .gitignore covers.
part = os.path.join(os.path.dirname(cfg), ".ergosphere.json.install.tmp")
try:
    os.makedirs(os.path.dirname(cfg), exist_ok=True)
    with open(part, "w", encoding="utf-8") as f:
        f.write(json.dumps(d, indent=2) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(part, cfg)
except OSError as exc:
    bad("could not write %s (%s)" % (cfg, exc))
if load(cfg) != d:
    bad("read-back: %s does not hold the intended settings" % cfg)
print("wrote")' "$@" </dev/null
}

# The file the server reads: ERGOSPHERE_CONFIG when set (relative to the
# server's cwd, the install dir), else ergosphere.json next to ergosphere.py.
# A legacy cwd-defaults.json is looked for next to it, as the server does.
set_config_file() {
  local c=${ERGOSPHERE_CONFIG:-}
  if [[ -z "$c" ]]; then
    CFG_FILE="$DEST/ergosphere.json"
  else
    if [[ "$c" == \~ || "$c" == \~/* ]]; then c="${HOME:-}${c:1}"; fi
    if [[ "$c" != /* ]]; then c="$DEST/$c"; fi
    CFG_FILE=$c
  fi
  LEGACY_FILE="${CFG_FILE%/*}/cwd-defaults.json"
}

# Sets CFG_SOURCE (json, legacy or new), CFG_BIND, CFG_PORT, CFG_AGENTS and
# AGENT_STATE (none, new, same, differs) from the config on disk.
read_config() {
  local out k v
  out=$(cfg_py read "$CFG_FILE" "$LEGACY_FILE" "$AGENT" "$AGENT_DIR" 2>&1) ||
    fail check "$out"
  while IFS=$'\t' read -r k v; do
    case "$k" in
      source) CFG_SOURCE=$v ;;
      bind) CFG_BIND=$v ;;
      port) CFG_PORT=$v ;;
      agents) CFG_AGENTS=$v ;;
      agent) AGENT_STATE=$v ;;
    esac
  done <<<"$out"
}

# Resolves bind and port: --bind/--port (or ERGOSPHERE_PORT) > ergosphere.json
# > defaults. OLD_* is where a server from this install should be listening now.
load_config() {
  set_config_file
  read_config
  set_bind "${CFG_BIND:-127.0.0.1}"
  set_port "${CFG_PORT:-9241}"
  OLD_BIND=$BIND OLD_PORT=$PORT
  if [[ -n "$ARG_BIND" ]]; then set_bind "$ARG_BIND"; fi
  if [[ -n "$ARG_PORT" ]]; then set_port "$ARG_PORT"; fi
  if [[ "$CFG_SOURCE" != json ]]; then OLD_BIND=$BIND OLD_PORT=$PORT; fi
}

# Checks that depend on the chosen settings. Still before any download.
check_target() {
  local cfg=$CFG_FILE up old=0
  printf 'Checking install target:\n'
  if [[ -e "$DEST" && ! -d "$DEST" ]]; then
    row invalid dir "$DEST exists and is not a directory"
  elif [[ -d "$DEST" && ( ! -w "$DEST" || ! -x "$DEST" ) ]]; then
    row invalid dir "$DEST is not writable"
  elif [[ -d "$DEST" && ! -f "$DEST/ergosphere.py" && ! -f "$DEST/ergosphere.json" && ! -f "$DEST/cwd-defaults.json" && -n "$(ls -A -- "$DEST")" ]]; then
    row invalid dir "$DEST is not empty and is not an Ergosphere install"
  elif [[ -d "$DEST" ]]; then
    row ok dir "$DEST exists"
  else
    up=$DEST
    while [[ ! -e "$up" ]]; do up=${up%/*}; up=${up:-/}; done
    if [[ -d "$up" && -w "$up" && -x "$up" ]]; then
      row ok dir "$DEST will be created"
    else
      row invalid dir "cannot create $DEST ($up is not writable)"
    fi
  fi
  read_config
  if [[ -n "$AGENT" ]]; then
    if [[ ! "$AGENT" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]]; then
      row invalid agent "name must be letters, digits, . _ - : $AGENT"
    elif [[ "$AGENT_DIR" != /* || ! -d "$AGENT_DIR" || "$AGENT_DIR" =~ [\"\\[:cntrl:]] ]]; then
      row invalid agent "working directory must be an existing absolute directory without quotes or backslashes"
    else
      case "$AGENT_STATE" in
        same) row ok agent "$AGENT=$AGENT_DIR already set" ;;
        differs) row warn agent "$AGENT not changed, use Settings (the config has another directory for it)" ;;
        *) row ok agent "$AGENT=$AGENT_DIR will be added" ;;
      esac
    fi
  fi
  case "$CFG_SOURCE" in
    json) row ok config "$cfg present: agents $CFG_AGENTS" ;;
    legacy) row ok config "missing; will be created from cwd-defaults.json (kept as is): agents $CFG_AGENTS" ;;
    *) row ok config "missing; will be created" ;;
  esac
  if (( PORT < 1024 )) && [[ "$EUID" -ne 0 ]]; then
    row invalid port "$PORT is privileged; pick a port above 1023"
  fi
  if [[ "$BIND" != 0.0.0.0 ]] && ! is_loopback "$BIND" && ! "$PYTHON" -I -c '
import socket, sys
socket.socket().bind((sys.argv[1], 0))' "$BIND" 2>/dev/null; then
    fail check "$BIND is not an address of this machine"
  fi
  # A bind/port change: the server from this install still listens on the
  # saved address and is restarted later, so only the new address must be free.
  if ! same_addr "$OLD_BIND:$OLD_PORT" "$BIND:$PORT"; then
    probe "$OLD_BIND" "$OLD_PORT"
    if [[ "$PROBE" == match || "$PROBE" == moved ]]; then
      same_install
      old=1
      row ok server "Ergosphere from this dir running on ${PROBE_AT}, pid $PROBE_PID; it moves to ${BIND}:${PORT}"
    fi
  fi
  if [[ "$old" -eq 0 || "$OLD_PORT" != "$PORT" ]]; then
    probe "$BIND" "$PORT"
    # With old=1 this install's server was already found and reported above.
    if [[ "$PROBE" == down && "$old" -eq 0 ]]; then probe_own; fi
    if [[ "$PROBE" == match || "$PROBE" == moved ]]; then same_install; fi
    case "$PROBE" in
      match) row ok server "Ergosphere from this dir already running on ${BIND}:${PORT}, pid $PROBE_PID" ;;
      moved) row ok server "Ergosphere from this dir running on ${PROBE_AT}, pid $PROBE_PID; it moves to ${BIND}:${PORT}" ;;
      down) row ok server "port $PORT is free" ;;
      *) row invalid server "port $PORT is in use by something that is not this Ergosphere at ${BIND}; it was not touched" ;;
    esac
  fi
  if is_loopback "$BIND"; then
    row ok bind "$BIND (loopback)"
  else
    row warn bind "$BIND: anyone who can reach port $PORT can run grok as this user"
  fi
  check_unit
  find_release
  if [[ ${#BLOCKERS[@]} -gt 0 ]]; then
    fail check "blocked by ${BLOCKERS[*]} (see above); nothing was changed"
  fi
}

# The release to install: --version, else the tag that GitHub's
# releases/latest page redirects to (a plain redirect, no API rate limit).
find_release() {
  local url="${ERGOSPHERE_INSTALL_LATEST_URL:-https://github.com/JuliusPIV/ergosphere/releases/latest}" loc
  if [[ "$PINNED" -eq 1 ]]; then
    row ok release "$RELEASE (--version)"
  elif [[ -n "${ERGOSPHERE_INSTALL_SOURCE_URL:-}" ]]; then
    row ok release "from ERGOSPHERE_INSTALL_SOURCE_URL"
    return 0
  else
    loc=$("$CURL" --proto '=http,https' -fsS --connect-timeout 15 --max-time 30 -o /dev/null \
      -w '%{redirect_url}' -- "$url" 2>/dev/null) || loc=""
    RELEASE=${loc##*/tag/}
    if [[ "$RELEASE" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
      row ok release "$RELEASE (latest)"
    else
      row missing release "could not find the latest release at $url; pass --version vA.B.C"
      return 0
    fi
  fi
  SOURCE_URL="https://github.com/JuliusPIV/ergosphere/archive/refs/tags/${RELEASE}.tar.gz"
}

# Only a bind chosen in this run is confirmed; a saved one was confirmed when set.
approve_bind() {
  if [[ -z "$ARG_BIND" ]] || is_loopback "$BIND" || [[ "$ASSUME_YES" -eq 1 ]]; then
    return 0
  fi
  tty_ok || fail check "non-loopback bind $BIND needs confirmation on a terminal, or --yes"
  printf 'Warning: anyone who can reach %s:%s can run grok as this user. Continue? [y/N] ' "$BIND" "$PORT" >/dev/tty
  IFS= read -r REPLY </dev/tty || REPLY=
  case "${REPLY%$'\r'}" in
    y|Y|yes|YES) ;;
    *) fail check "non-loopback bind $BIND was not approved; nothing was changed" ;;
  esac
}

download() {
  local url="${ERGOSPHERE_INSTALL_SOURCE_URL:-$SOURCE_URL}" hits types
  STAGE=$(mktemp -d "${TMPDIR:-/tmp}/ergosphere-install.XXXXXX") || fail download "could not create a temp dir"
  printf 'download: %s\n' "$url"
  "$CURL" --proto '=http,https' -fsSL --connect-timeout 15 --max-time 120 \
    -o "$STAGE/src.tar.gz" -- "$url" || fail download "could not fetch $url"
  # Only plain files and dirs: a link could point outside the install dir.
  # GNU tar itself strips leading / and refuses .. members.
  types=$(tar -tvzf "$STAGE/src.tar.gz" 2>/dev/null | cut -c1 | sort -u | tr -d '\n') ||
    fail download "archive is damaged or truncated"
  [[ "$types" =~ ^[-d]+$ ]] || fail download "archive holds links or special files"
  mkdir "$STAGE/x"
  tar -xzf "$STAGE/src.tar.gz" -C "$STAGE/x" --no-same-owner 2>/dev/null ||
    fail download "archive is damaged or truncated"
  hits=("$STAGE"/x/*/ergosphere.py)
  [[ ${#hits[@]} -eq 1 && -f "${hits[0]}" ]] || fail download "archive does not hold one ergosphere.py"
  SRC=${hits[0]%/ergosphere.py}
}

# Copies only files whose sha256 differs from the archive, then reads each
# file back by hash. The user's config and log are never in this set.
install_files() {
  local rel n=0 files=0
  if [[ ! -d "$DEST" ]]; then
    mkdir -p -- "$DEST" || fail install "could not create $DEST"
    CHANGED+=("created $DEST")
  fi
  while IFS= read -r -d '' rel; do
    rel=${rel#./}
    if [[ "$rel" == cwd-defaults.json || "$rel" == ergosphere.json || "$rel" == ergosphere.log ]]; then continue; fi
    if [[ "$(sha_of "$SRC/$rel")" != "$(sha_of "$DEST/$rel")" ]]; then
      { mkdir -p -- "$DEST/$(dirname -- "$rel")" &&
        cp -p -- "$SRC/$rel" "$DEST/$rel.part" &&
        mv -f -- "$DEST/$rel.part" "$DEST/$rel"; } 2>/dev/null || fail install "could not write $DEST/$rel"
      printf 'install: updated %s\n' "$rel"
      n=$((n + 1))
      if [[ "$(sha_of "$SRC/$rel")" != "$(sha_of "$DEST/$rel")" ]]; then
        fail install "read-back: $DEST/$rel does not match the archive"
      fi
    fi
    files=$((files + 1))
  done < <(cd "$SRC" && find . -type f -print0 | sort -z)
  [[ "$files" -gt 0 ]] || fail install "archive has no files"
  printf 'install: %s package files match the archive by sha256 (%s updated)\n' "$files" "$n"
  CODE_UPDATED=$n
  if [[ "$n" -gt 0 ]]; then CHANGED+=("$n package files"); fi
}

# Creates ergosphere.json, or updates only the keys this run sets: bind/port
# when given, a manual agent when its name is new. cwd-defaults.json is never
# changed. The result is read back.
write_config() {
  local cfg=$CFG_FILE out sb=0 sp=0
  if [[ -n "$ARG_BIND" ]]; then sb=1; fi
  if [[ -n "$ARG_PORT" ]]; then sp=1; fi
  if [[ "$AGENT_STATE" == differs ]]; then
    CFG_NOTE="agent ${AGENT} not changed, use Settings ($cfg has another directory for it)"
  fi
  if [[ -n "$AGENT" && "$AGENT_STATE" == new && "$CFG_SOURCE" == json ]]; then AGENT_ADDED=1; fi
  out=$(cfg_py write "$cfg" "$LEGACY_FILE" "$BIND" "$PORT" "$sb" "$sp" "$AGENT" "$AGENT_DIR" 2>&1) ||
    fail install "$out"
  read_config
  [[ "$CFG_SOURCE" == json && ( -z "$CFG_BIND" || "${CFG_BIND,,}" == "${BIND,,}" ) &&
     ( -z "$CFG_PORT" || "$CFG_PORT" == "$PORT" ) ]] ||
    fail install "read-back: $cfg does not hold bind $BIND and port $PORT"
  if [[ -n "$AGENT" && "$AGENT_STATE" == new ]]; then
    fail install "read-back: agent $AGENT is not in $cfg"
  fi
  if [[ "$out" == wrote ]]; then
    printf 'config: wrote %s (read back: bind %s, port %s, agents %s)\n' "$cfg" "$BIND" "$PORT" "$CFG_AGENTS"
    CHANGED+=("config")
  else
    printf 'config: %s already matches\n' "$cfg"
  fi
}

# True when the server pid was started with --bind or --port arguments.
has_addr_args() {
  local arg
  while IFS= read -r -d '' arg; do
    if [[ "$arg" == --bind* || "$arg" == --port* ]]; then return 0; fi
  done <"/proc/$1/cmdline"
  return 1
}

# The server reads bind and port from ergosphere.json, so it runs with no
# address arguments and without ERGOSPHERE_PORT, which would override the file.
start_server() {
  local out deadline kids why="" main=""
  out=$(cd -- "$DEST" && unset ERGOSPHERE_PORT && "$PYTHON" ergosphere.py --check </dev/null 2>&1) ||
    fail install "ergosphere.py --check failed: ${out//$'\n'/; }"
  SRV_BIND=$BIND SRV_PORT=$PORT
  probe "$OLD_BIND" "$OLD_PORT"
  if [[ "$PROBE" != match && "$PROBE" != moved ]] && ! same_addr "$OLD_BIND:$OLD_PORT" "$BIND:$PORT"; then probe "$BIND" "$PORT"; fi
  if [[ "$PROBE" == down ]]; then probe_own; fi
  if [[ "$PROBE" == match || "$PROBE" == moved ]]; then
    same_install
    PID=$PROBE_PID
    main=""
    if [[ "$WANT_UNIT" -eq 1 ]]; then main=$(unit_main_pid); fi
    if [[ "$PROBE" == moved ]] || ! same_addr "$PROBE_AT" "$BIND:$PORT"; then
      why="settings moved it from $PROBE_AT to ${BIND}:${PORT}"
    elif server_stale; then
      why="runtime differs"
    elif has_addr_args "$PID"; then
      why="it was started with --bind/--port; it now reads ergosphere.json"
    elif [[ "$AGENT_ADDED" -eq 1 ]]; then
      # The server reads agents at startup.
      why="agent $AGENT was added"
    elif [[ "$WANT_UNIT" -eq 1 && "$PID" != "$main" ]]; then
      why="ergosphere.service takes it over"
    elif [[ "$WANT_UNIT" -eq 1 && "$UNIT_CHANGED" -eq 1 ]]; then
      why="ergosphere.service changed"
    fi
    if [[ -z "$why" ]]; then
      printf 'start: already running, pid %s (read back from /health)\n' "$PID"
      return 0
    fi
    # A restart kills its grok sessions, so only restart a server that has none.
    kids=$(child_count "$PID")
    if [[ "$kids" -gt 0 ]]; then
      local noun="grok sessions"
      if [[ "$kids" -eq 1 ]]; then noun="grok session"; fi
      SRV_BIND=${PROBE_AT%:*} SRV_PORT=${PROBE_AT##*:}
      SERVER_NOTE="Ergosphere pid $PID keeps running as before ($why) because it has $kids $noun open; when they are done run: kill $PID, then rerun this installer"
      if [[ "$WANT_UNIT" -eq 1 && "$PID" == "$main" ]]; then
        SERVER_NOTE="Ergosphere pid $PID keeps running as before ($why) because it has $kids $noun open; when they are done run: systemctl --user restart ergosphere"
      elif [[ "$WANT_UNIT" -eq 1 ]]; then
        SERVER_NOTE="Ergosphere pid $PID keeps running without systemd because it has $kids $noun open; ergosphere.service is enabled but cannot start while pid $PID holds the port (at login it tries 5 times, then gives up). When they are done run: kill $PID; systemctl --user reset-failed ergosphere; systemctl --user start ergosphere"
      fi
      printf 'start: already running, pid %s, not restarted (%s %s open): %s\n' "$PID" "$kids" "$noun" "$why"
      return 0
    fi
    printf 'start: restarting pid %s (no grok sessions): %s\n' "$PID" "$why"
    if [[ "$WANT_UNIT" -eq 1 && "$PID" == "$main" ]]; then
      start_unit restart
      return 0
    fi
    kill "$PID" 2>/dev/null || true
    deadline=$((SECONDS + 10))
    while kill -0 "$PID" 2>/dev/null && (( SECONDS < deadline )); do sleep 0.2; done
    if kill -0 "$PID" 2>/dev/null; then fail start "pid $PID did not stop; it was left running"; fi
    CHANGED+=("stopped old server $PID")
    probe "$BIND" "$PORT"
  fi
  [[ "$PROBE" == down ]] || fail start "port $PORT is in use by something that is not this Ergosphere; it was not touched"
  if [[ "$WANT_UNIT" -eq 1 ]]; then
    start_unit start
    return 0
  fi
  # cwd is the install dir. The server finds grok with shutil.which, so
  # keep grok's dir on PATH. exec keeps $! as the server's pid.
  (
    cd -- "$DEST" || exit 1
    unset ERGOSPHERE_PORT
    export PATH="${GROK%/*}:$PATH"
    exec nohup "$PYTHON" "$DEST/ergosphere.py"
  ) >>"$DEST/ergosphere.log" 2>&1 </dev/null &
  STARTED_PID=$!
  deadline=$((SECONDS + 10))
  while (( SECONDS < deadline )); do
    probe "$BIND" "$PORT"
    if [[ "$PROBE" == match && "$PROBE_PID" == "$STARTED_PID" ]]; then
      [[ "$(readlink "/proc/$STARTED_PID/cwd")" == "$(realpath -- "$DEST")" ]] ||
        fail start "server pid $STARTED_PID is not running in $DEST"
      ! has_addr_args "$STARTED_PID" || fail start "server pid $STARTED_PID has --bind/--port arguments"
      VERIFIED=1 PID=$STARTED_PID
      printf 'start: started pid %s in %s on %s:%s from ergosphere.json (read back from /health and /proc)\n' "$PID" "$DEST" "$BIND" "$PORT"
      CHANGED+=("server started")
      return 0
    fi
    kill -0 "$STARTED_PID" 2>/dev/null || break
    sleep 0.2
  done
  tail -n 20 -- "$DEST/ergosphere.log" >&2 || true
  fail start "server did not answer /health as pid $STARTED_PID on ${BIND}:${PORT}"
}

summary() {
  local url="http://${SRV_BIND}:${SRV_PORT}/" args=() base=() rerun env="" c list="" a addrs=()
  local BIND=$SRV_BIND PORT=$SRV_PORT
  if [[ "$BIND" == 0.0.0.0 ]]; then url="http://localhost:${PORT}/"; fi
  # Bind and port are in ergosphere.json, so the rerun line needs only --dir.
  if [[ "$DEST" != "$DEFAULT_DEST" ]]; then base+=(--dir "$DEST"); fi
  if [[ "$PINNED" -eq 1 ]]; then base+=(--version "$RELEASE"); fi
  args=(${base[@]+"${base[@]}"})
  if [[ -n "${ERGOSPHERE_INSTALL_SOURCE_URL:-}" ]]; then
    env="ERGOSPHERE_INSTALL_SOURCE_URL=$(printf '%q' "$ERGOSPHERE_INSTALL_SOURCE_URL") "
  fi
  rerun="curl -fsSL ${SCRIPT_URL} | ${env}bash"
  if [[ ${#args[@]} -gt 0 ]]; then rerun+=" -s -- --auto$(printf ' %q' "${args[@]}")"; fi
  printf '\nErgosphere is running.\n'
  printf '  url:      %s\n' "$url"
  printf '  bind:     %s\n' "$BIND"
  printf '  port:     %s\n' "$PORT"
  printf '  dir:      %s\n' "$DEST"
  printf '  config:   %s\n' "$CFG_FILE"
  printf '            Change settings in the web page under Settings, or rerun with --bind/--port.\n'
  printf '  agents:   %s\n' "$CFG_AGENTS"
  if [[ -n "$CFG_NOTE" ]]; then printf '  note:     %s\n' "$CFG_NOTE"; fi
  if [[ -n "$SERVER_NOTE" ]]; then printf '  note:     %s\n' "$SERVER_NOTE"; fi
  printf '  mode:     %s\n' "$MODE"
  printf '  log:      %s\n' "$DEST/ergosphere.log"
  printf '  pid:      %s\n' "$PID"
  if [[ "$WANT_UNIT" -eq 1 ]]; then
    if [[ "$UNIT_TYPE" == notify ]]; then
      printf '  service:  %s (enabled: starts at login, restarts if it crashes or hangs)\n' "$UNIT_FILE"
    else
      printf '  service:  %s (enabled: starts at login, restarts if it crashes; this release has no watchdog support, so a hang is not restarted)\n' "$UNIT_FILE"
    fi
    if [[ "$(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null || true)" != yes ]]; then
      printf '            To start at boot before you log in: loginctl enable-linger %s (may need sudo)\n' "$(id -un)"
    fi
  fi
  if [[ "$WANT_UNIT" -eq 1 && "$PID" == "$(unit_main_pid)" ]]; then
    printf '  stop:     systemctl --user stop ergosphere   (kill restarts it; stopping also ends its grok sessions)\n'
  elif [[ "$WANT_UNIT" -eq 1 ]]; then
    # Deferred takeover: the plain server still serves; the unit cannot start until it stops.
    printf '  stop:     kill %s   (not run by systemd yet; see the note for starting ergosphere.service after it)\n' "$PID"
  else
    printf '  stop:     kill %s   (also stops the grok sessions it started; closing a tab does not)\n' "$PID"
  fi
  printf '  rerun:    %s\n' "$rerun"
  for c in "${CHANGED[@]}"; do list+="${list:+, }$c"; done
  if [[ -z "$list" && -n "$SERVER_NOTE" ]]; then list="nothing; the running server is not updated yet (see note)"; fi
  printf '  changed:  %s\n' "${list:-nothing (files, config and server already matched)}"
  printf '  grok must already be signed in as this user.\n'
  printf '  access:\n'
  if is_loopback "$BIND"; then
    a=$BIND
    if [[ "$a" == localhost ]]; then a=127.0.0.1; fi
    printf '    This page is only reachable on this machine. From another computer, tunnel it:\n'
    printf '      ssh -N -L %s:%s:%s %s@%s\n' "$PORT" "$a" "$PORT" "$(id -un)" "$(hostname)"
    printf '    and open http://localhost:%s/\n' "$PORT"
    printf '    To listen on the network instead, rerun:\n'
    printf '      curl -fsSL %s | %sbash -s -- --auto%s\n' "$SCRIPT_URL" "$env" \
      "$(printf ' %q' ${base[@]+"${base[@]}"} --bind 0.0.0.0 --yes)"
    return 0
  fi
  if [[ "$BIND" == 0.0.0.0 ]]; then
    for a in $(hostname -I 2>/dev/null); do
      if [[ "$a" =~ ^[0-9.]+$ ]] && ! is_loopback "$a"; then addrs+=("$a"); fi
    done
  else
    addrs=("$BIND")
  fi
  for a in ${addrs[@]+"${addrs[@]}"}; do printf '    http://%s:%s/\n' "$a" "$PORT"; done
  if [[ ${#addrs[@]} -eq 0 ]]; then printf '    no IPv4 address found; use this machine'"'"'s address on port %s\n' "$PORT"; fi
  printf '    There is no login: anyone who can reach port %s can run grok as this user.\n' "$PORT"
}

main() {
  local arg
  if [ -z "${BASH_VERSION:-}" ]; then
    echo "failed at check: run this installer with bash" >&2
    exit 1
  fi
  set -euo pipefail
  RELEASE="" PINNED=0 SOURCE_URL=""
  SCRIPT_URL="https://raw.githubusercontent.com/JuliusPIV/ergosphere/main/install.sh"
  DEFAULT_DEST="${HOME:-}/.local/share/ergosphere"
  DEST=$DEFAULT_DEST BIND=127.0.0.1 PORT=9241 ARG_BIND="" ARG_PORT="" MODE="" WANT_CHECK=0 ASSUME_YES=0
  OLD_BIND="" OLD_PORT="" SRV_BIND="" SRV_PORT="" CFG_SOURCE="" CFG_BIND="" CFG_PORT="" AGENT_STATE=none AGENT_ADDED=0
  AGENT="" AGENT_DIR="" CFG_FILE="" LEGACY_FILE="" CFG_AGENTS="(none)" CFG_NOTE="" SERVER_NOTE="" PID=""
  STAGE="" SRC="" STARTED_PID="" VERIFIED=0 PROBE="" PROBE_PID="" PROBE_DIGEST="" PROBE_AT=""
  PYTHON="" CURL="" GROK="" BLOCKERS=() CHANGED=()
  SERVICE="" UNIT_FILE="" UNIT_FOREIGN="" UNIT_DROPINS="" UNIT_STATE="" UNIT_TYPE=notify WANT_UNIT=0 UNIT_CHANGED=0 UNIT_STARTED=0 UNIT_WRITTEN=0 UNIT_HAD_FILE=0 UNIT_WAS_ACTIVE=0 CODE_UPDATED=0
  export PYTHONDONTWRITEBYTECODE=1
  trap cleanup EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM HUP
  # Help must work even when the surrounding installation settings are invalid.
  for arg in "$@"; do
    if [[ "$arg" == --help ]]; then usage; return 0; fi
  done
  parse_args "$@"
  # An explicit --port wins, so a bad ERGOSPHERE_PORT is only checked when used.
  # ERGOSPHERE_PORT counts as a given port, as it does for the server.
  if [[ -z "$ARG_PORT" && -n "${ERGOSPHERE_PORT:-}" ]]; then set_port "$ERGOSPHERE_PORT"; ARG_PORT=$PORT; fi
  check_host
  if [[ "$WANT_CHECK" -eq 0 ]]; then
    choose_mode
    if [[ "$MODE" == manual ]]; then ask_dir; fi
  fi
  [[ -n "${HOME:-}" || "$DEST" != "$DEFAULT_DEST" ]] || fail check "HOME is not set; pass --dir"
  load_config
  if [[ "$WANT_CHECK" -eq 0 && "$MODE" == manual ]]; then ask_manual; fi
  check_target
  if [[ "$WANT_CHECK" -eq 1 ]]; then
    printf 'ready: checks passed, nothing changed\n'
    return 0
  fi
  approve_bind
  download
  install_files
  write_config
  remove_unit
  install_unit
  start_server
  enable_unit
  summary
}

main "$@"; }