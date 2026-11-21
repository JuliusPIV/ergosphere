#!/usr/bin/env bash
# install.sh end to end (issue #95), in one throwaway HOME and install dir:
#   1. fresh install: /health answers with the package's code fingerprint;
#   2. a rerun of the same package changes nothing (same pid, same files);
#   3. an upgrade to a newer package: /health shows the new fingerprint;
#   4. a failed upgrade (damaged archive, a release whose --check fails):
#      the old server still runs and the old code is still in place.
# Not covered: a release that passes --check but fails to start. Today that
# leaves the old server stopped and the new code in place (#29, rollback not
# implemented yet); add the case here when #29 lands.
#
# Run: ERGOSPHERE_TEST_PORT=<spare port> bash tests/install_e2e_test.sh
# No network, no git: packages are served from 127.0.0.1 and grok is a stub
# that only answers --version. Every server it starts is stopped at the end.
set -u
repo=$(cd "$(dirname "$0")/.." && pwd)
port=${ERGOSPHERE_TEST_PORT:-$(python3 -I "$repo/tests/free_port.py" 3)} || exit 1
work=$(mktemp -d "${TMPDIR:-/tmp}/ergosphere-e2e-test.XXXXXX")
home="$work/u" dest="$work/u/app"
pids=()
fail=0

# shellcheck disable=SC2317  # run by the EXIT trap
cleanup() {
  local p
  # One SIGTERM per pid (track keeps them unique), then up to 10 s for them
  # to go, then SIGKILL, so no server outlives the test or holds the port.
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

# pack NAME [LINE]: a release archive with one top folder; LINE is appended
# to ergosphere.py, so each version has its own code fingerprint.
pack() {
  local d="$work/pkg-$1/ergosphere-test"
  mkdir -p "$d"
  cp -p "$repo/ergosphere.py" "$repo/LICENSE" "$d/"
  cp -rp "$repo/vendor" "$d/"
  if [[ -n "${2:-}" ]]; then printf '%s\n' "$2" >>"$d/ergosphere.py"; fi
  tar -czf "$work/www/$1.tar.gz" -C "$work/pkg-$1" ergosphere-test
}
mkdir -p "$work/bin" "$work/www" "$home"
pack v1
pack v2 "# install_e2e_test: version 2"
# A release whose own ergosphere.py --check fails: it exits on its first line.
pack nocheck
sed -i '1i raise SystemExit("broken release")' "$work/pkg-nocheck/ergosphere-test/ergosphere.py"
tar -czf "$work/www/nocheck.tar.gz" -C "$work/pkg-nocheck" ergosphere-test
head -c 2000 "$work/www/v2.tar.gz" >"$work/www/truncated.tar.gz"
printf '#!/bin/sh\necho "grok 0.0.0-test"\n' >"$work/bin/grok"
chmod +x "$work/bin/grok"

# The server's runtime fingerprint, computed here from the package files
# (sha256 of each file's sha256, in this order), not by the code under test.
fingerprint() {
  python3 -I -c '
import hashlib, sys
d = hashlib.sha256()
for n in ("ergosphere.py", "vendor/addon-fit.js", "vendor/xterm.css", "vendor/xterm.js"):
    d.update(hashlib.sha256(open(sys.argv[1] + "/" + n, "rb").read()).digest())
print(d.hexdigest())' "$1"
}
fp1=$(fingerprint "$work/pkg-v1/ergosphere-test")
fp2=$(fingerprint "$work/pkg-v2/ergosphere-test")
sha_py() { sha256sum <"$1/ergosphere.py" | cut -d' ' -f1; }
py2=$(sha_py "$work/pkg-v2/ergosphere-test")

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

# inst PKG: runs the installer on that package; prints its output.
# setsid: no controlling terminal, so nothing can be answered on /dev/tty.
inst() {
  env -u ERGOSPHERE_PORT -u ERGOSPHERE_CONFIG HOME="$home" PATH="$work/bin:$PATH" TMPDIR="$work" \
    ERGOSPHERE_INSTALL_SOURCE_URL="$www/$1.tar.gz" \
    setsid -w bash "$repo/install.sh" --auto --dir "$dest" --port "$port" </dev/null 2>&1
}
field() { sed -n "s/^  $1: *//p" <<<"$2" | head -n 1; }
# health: prints "pid code_sha256" from /health on the test port, or nothing.
health() {
  curl --noproxy '*' -s --max-time 3 "http://127.0.0.1:$port/health" |
    python3 -I -c 'import json,sys; d=json.load(sys.stdin); print(d["pid"], d["code_sha256"]) if d.get("product") == "ergosphere" else None' 2>/dev/null
}
# track PID: stop it at the end. Each pid once: back-to-back SIGTERMs can
# leave the server hung (#216); one is what a user sends.
track() {
  [[ -n "$1" && " ${pids[*]} " != *" $1 "* ]] && pids+=("$1")
  return 0
}
# installed_sha: one hash over every file in the install dir except the log,
# which the server appends to.
installed_sha() {
  (cd "$dest" && find . -type f ! -name ergosphere.log -print0 | sort -z | xargs -0 sha256sum) | sha256sum
}

# 1. Fresh install.
out=$(inst v1); rc=$?
pid1=$(field pid "$out")
track "$pid1"
if [[ "$rc" -eq 0 && -n "$pid1" && "$(health)" == "$pid1 $fp1" ]]; then
  ok "fresh install: pid $pid1 answers /health with the v1 fingerprint"
else
  bad "fresh install: rc $rc, pid '${pid1:-none}', /health '$(health)', want '$pid1 $fp1'"
  printf '%s\n' "$out" | tail -n 15
  exit 1
fi

# 2. Rerun the same package.
before=$(installed_sha)
out=$(inst v1); rc=$?
track "$(field pid "$out")"
if [[ "$rc" -eq 0 && "$(field pid "$out")" == "$pid1" && "$(field changed "$out")" == nothing* &&
      "$(health)" == "$pid1 $fp1" && "$(installed_sha)" == "$before" ]]; then
  ok "rerun: same pid $pid1, nothing changed"
else
  bad "rerun: rc $rc, pid '$(field pid "$out")', changed '$(field changed "$out")', /health '$(health)'"
  printf '%s\n' "$out" | tail -n 15
fi

# 3. Upgrade to v2.
out=$(inst v2); rc=$?
pid2=$(field pid "$out")
track "$pid2"
if [[ "$rc" -eq 0 && -n "$pid2" && "$pid2" != "$pid1" && "$(health)" == "$pid2 $fp2" ]] &&
    ! kill -0 "$pid1" 2>/dev/null && [[ "$(sha_py "$dest")" == "$py2" ]]; then
  ok "upgrade: pid $pid1 -> $pid2, /health shows the v2 fingerprint"
else
  bad "upgrade: rc $rc, pid '${pid2:-none}', /health '$(health)', want '$pid2 $fp2'"
  printf '%s\n' "$out" | tail -n 15
  exit 1
fi

# 4. Failed upgrades: refused, the v2 server and files untouched.
before=$(installed_sha)
for pkg in truncated nocheck; do
  out=$(inst "$pkg"); rc=$?
  if [[ "$rc" -ne 0 && "$out" == *"failed at "* ]]; then
    ok "failed upgrade ($pkg): installer exits $rc ($(grep -m1 '^failed at' <<<"$out"))"
  else
    bad "failed upgrade ($pkg): rc $rc, want a failure"
    printf '%s\n' "$out" | tail -n 15
  fi
  if kill -0 "$pid2" 2>/dev/null && [[ "$(health)" == "$pid2 $fp2" ]]; then
    ok "failed upgrade ($pkg): old server pid $pid2 still serves the v2 code"
  else
    bad "failed upgrade ($pkg): /health '$(health)', want '$pid2 $fp2'"
  fi
  if [[ "$(installed_sha)" == "$before" ]]; then
    ok "failed upgrade ($pkg): install dir unchanged"
  else
    bad "failed upgrade ($pkg): install dir changed"
  fi
done

exit "$fail"
