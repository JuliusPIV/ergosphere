#!/usr/bin/env bash
# install.sh --check stops where the real run would stop, and says what the
# real run would do with the server (issue #140):
#   1. --bind 0.0.0.0 with no --yes and no terminal: --check is refused, as
#      the real run is (approve_bind); with --yes it passes;
#   2. --check --manual with no terminal: refused, as the real run is (the
#      questions are not skipped silently);
#   3. --check prints the planned server action and changes nothing:
#      start (no server), keep (same release), restart (new release), keep
#      because a grok session is open; each matches what the real run then does.
# --check still runs nothing from the download (#168): see
# install_verify_release_test.sh.
#
# Run: ERGOSPHERE_TEST_PORT=<spare port> bash tests/install_check_plan_test.sh
# ERGOSPHERE_TEST_INSTALLER=<path> runs another install.sh on this tree's
# packages (the A/B run uses it for the base side).
# No network, no git: packages are served from 127.0.0.1 and grok is a stub
# that answers --version and otherwise sleeps. Every process it starts is
# stopped at the end.
set -u
repo=$(cd "$(dirname "$0")/.." && pwd)
port=${ERGOSPHERE_TEST_PORT:-$(python3 -I "$repo/tests/free_port.py" 3)} || exit 1
installer=${ERGOSPHERE_TEST_INSTALLER:-$repo/install.sh}
work=$(mktemp -d "${TMPDIR:-/tmp}/ergosphere-plan-test.XXXXXX")
home="$work/u" dest="$work/u/app"
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
  printf '# install_check_plan_test: %s\n' "$1" >>"$d/ergosphere.py"
  tar -czf "$work/www/$1.tar.gz" -C "$work/pkg-$1" ergosphere-test
}
mkdir -p "$work/bin" "$work/www" "$home"
for v in v1 v2 v3; do pack "$v"; done
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

# inst PKG [installer args...]: prints the output, returns its rc.
# setsid: no controlling terminal, so nothing can be answered on /dev/tty.
inst() {
  local pkg=$1
  shift
  env -u ERGOSPHERE_PORT -u ERGOSPHERE_CONFIG HOME="$home" PATH="$work/bin:$PATH" TMPDIR="$work" \
    ERGOSPHERE_INSTALL_SOURCE_URL="$www/$pkg.tar.gz" \
    setsid -w bash "$installer" --dir "$dest" --port "$port" "$@" </dev/null 2>&1
}
field() { sed -n "s/^  $1: *//p" <<<"$2" | head -n 1; }
plan() { sed -n 's/^  ok *plan *//p' <<<"$1" | head -n 1; }
pid_now() {
  curl --noproxy '*' -s --max-time 3 "http://127.0.0.1:$port/health" |
    python3 -I -c 'import json,sys; print(json.load(sys.stdin)["pid"])' 2>/dev/null
}
track() {
  [[ -n "$1" && " ${pids[*]} " != *" $1 "* ]] && pids+=("$1")
  return 0
}
show() { printf '%s\n' "$1" | tail -n 12; }

# 1. A non-loopback bind needs approval in --check too.
out=$(inst v1 --check --bind 0.0.0.0); rc=$?
if [[ "$rc" -ne 0 && "$out" == *"needs confirmation on a terminal, or --yes"* && ! -e "$dest" ]]; then
  ok "--check --bind 0.0.0.0, no --yes, no terminal: refused"
else
  bad "--check --bind 0.0.0.0, no --yes, no terminal: rc $rc, want the approve_bind refusal"
  show "$out"
fi
out=$(inst v1 --check --bind 0.0.0.0 --yes); rc=$?
if [[ "$rc" -eq 0 && "$out" == *"ready: checks passed"* && ! -e "$dest" ]]; then
  ok "--check --bind 0.0.0.0 --yes: passes"
else
  bad "--check --bind 0.0.0.0 --yes: rc $rc"
  show "$out"
fi

# 2. --check --manual asks the manual questions, so with no terminal it fails
# as the real run does.
out=$(inst v1 --check --manual); rc=$?
if [[ "$rc" -ne 0 && "$out" == *"manual mode needs a terminal"* && ! -e "$dest" ]]; then
  ok "--check --manual, no terminal: refused"
else
  bad "--check --manual, no terminal: rc $rc, want 'manual mode needs a terminal'"
  show "$out"
fi

# 3. The planned server action, then the real run doing it.
out=$(inst v1 --check); rc=$?
p=$(plan "$out")
if [[ "$rc" -eq 0 && "$p" == "start a server on 127.0.0.1:$port" && ! -e "$dest" ]]; then
  ok "fresh: plan '$p'"
else
  bad "fresh: rc $rc, plan '${p:-none}'"
  show "$out"
fi
out=$(inst v1 --auto); rc=$?
pid1=$(field pid "$out")
track "$pid1"
if [[ "$rc" -ne 0 || -z "$pid1" || "$(pid_now)" != "$pid1" ]]; then
  bad "fresh install: rc $rc, pid '${pid1:-none}'"
  show "$out"
  exit 1
fi

out=$(inst v1 --check); rc=$?
p=$(plan "$out")
if [[ "$rc" -eq 0 && "$p" == "keep pid $pid1 running (already up to date)" && "$(pid_now)" == "$pid1" ]]; then
  ok "same release: plan '$p'"
else
  bad "same release: rc $rc, plan '${p:-none}'"
  show "$out"
fi

sha_before=$(sha256sum <"$dest/ergosphere.py")
out=$(inst v2 --check); rc=$?
p=$(plan "$out")
if [[ "$rc" -eq 0 && "$p" == "restart pid $pid1 (no grok sessions now): runtime differs" &&
    "$(pid_now)" == "$pid1" && "$(sha256sum <"$dest/ergosphere.py")" == "$sha_before" ]]; then
  ok "new release: plan '$p', nothing changed"
else
  bad "new release: rc $rc, plan '${p:-none}'"
  show "$out"
fi
out=$(inst v2 --auto); rc=$?
pid2=$(field pid "$out")
track "$pid2"
if [[ "$rc" -eq 0 && "$out" == *"start: restarting pid $pid1"* && -n "$pid2" && "$pid2" != "$pid1" ]]; then
  ok "new release real run: restarts pid $pid1 -> $pid2"
else
  bad "new release real run: rc $rc, pid $pid1 -> ${pid2:-none}"
  show "$out"
fi

# A grok session is open: the plan keeps the server, as the real run does.
curl --noproxy '*' -s --max-time 5 -H "Origin: http://127.0.0.1:$port" -H 'Content-Type: application/json' \
  --data "{\"approval\":\"ask\",\"cwd\":\"$work\"}" "http://127.0.0.1:$port/launch" >/dev/null
for _ in $(seq 100); do pgrep -P "$pid2" >"$work/grok.pid" && break; sleep 0.1; done
if [[ ! -s "$work/grok.pid" ]]; then
  bad "could not start a grok session under pid $pid2"
  exit 1
fi
out=$(inst v3 --check); rc=$?
p=$(plan "$out")
if [[ "$rc" -eq 0 && "$p" == "keep pid $pid2 running (1 grok session open): runtime differs" ]]; then
  ok "session open: plan '$p'"
else
  bad "session open: rc $rc, plan '${p:-none}'"
  show "$out"
fi
out=$(inst v3 --auto); rc=$?
if [[ "$rc" -eq 0 && "$out" == *"not restarted (1 grok session open)"* && "$(pid_now)" == "$pid2" ]]; then
  ok "session open real run: keeps pid $pid2"
else
  bad "session open real run: rc $rc, pid $pid2 -> $(pid_now)"
  show "$out"
fi

exit "$fail"
