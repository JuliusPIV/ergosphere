#!/usr/bin/env bash
# install.sh installs only what the server needs: the developer files a
# release archive also holds (tests/, docs/, .ghflow*, .git*, install.sh) stay
# out of the install dir (#97). The package is the checkout packed as a
# release archive, developer files included. /health still reports the
# runtime fingerprint and a rerun changes nothing.
#
# Run: ERGOSPHERE_TEST_PORT=<spare port> bash tests/install_dev_files_test.sh
# No network: the package is served from 127.0.0.1 and grok is a stub that
# only answers --version. The server it starts is stopped at the end.
set -u
repo=$(cd "$(dirname "$0")/.." && pwd)
port=${ERGOSPHERE_TEST_PORT:-$(python3 -I "$repo/tests/free_port.py" 3)} || exit 1
work=$(mktemp -d "${TMPDIR:-/tmp}/ergosphere-devfiles-test.XXXXXX")
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

mkdir -p "$work/bin" "$work/www" "$work/userhome"
# A tar of the checkout, not git archive: the test sandbox has no git.
mkdir -p "$work/pkg"
tar -c -C "$repo" --exclude=.git --exclude=__pycache__ --transform 's|^\.|ergosphere-test|' . |
  tar -x -C "$work/pkg" || exit 1
tar -czf "$work/www/src.tar.gz" -C "$work/pkg" ergosphere-test || exit 1
listing=$(tar -tzf "$work/www/src.tar.gz")
# Without developer files in the archive this test would prove nothing.
for want in ergosphere-test/tests/run_all.sh ergosphere-test/install.sh ergosphere-test/.ghflow.conf; do
  grep -qxF "$want" <<<"$listing" || { bad "archive lacks $want; the test cannot run"; exit 1; }
done
printf '#!/bin/sh\necho "grok 0.0.0-test"\n' >"$work/bin/grok"
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

app="$work/userhome/app"
run_install() {
  env -u ERGOSPHERE_PORT -u ERGOSPHERE_CONFIG HOME="$work/userhome" PATH="$work/bin:$PATH" TMPDIR="$work" \
    ERGOSPHERE_INSTALL_SOURCE_URL="$url" setsid -w bash "$repo/install.sh" --auto --dir "$app" --port "$port" </dev/null 2>&1
}
field() { sed -n "s/^  $1: *//p" <<<"$2" | head -n 1; }

out=$(run_install)
pid=$(field pid "$out")
if [[ -n "$pid" ]]; then pids+=("$pid"); fi
if ! grep -q '^Ergosphere is running' <<<"$out"; then
  bad "install did not finish"
  printf '%s\n' "$out" | tail -n 15
  exit 1
fi

files=$(cd "$app" && find . -type f | sed 's|^\./||' | sort)
dev=$(grep -E '^(tests/|docs/|\.git|\.ghflow|install\.sh$)' <<<"$files")
if [[ -z "$dev" ]]; then ok "install dir holds no developer files"; else bad "install dir holds developer files: $(tr '\n' ' ' <<<"$dev")"; fi
for want in ergosphere.py LICENSE vendor/xterm.js vendor/xterm.css vendor/addon-fit.js; do
  if grep -qxF "$want" <<<"$files"; then ok "installed $want"; else bad "$want is missing from the install dir"; fi
done

# The server runs from the install dir and reports the release's runtime fingerprint.
want_sha=$(cd "$app" && python3 -I -c 'import hashlib as h; print(h.sha256(b"".join(h.sha256(open(f, "rb").read()).digest() for f in ("ergosphere.py", "vendor/addon-fit.js", "vendor/xterm.css", "vendor/xterm.js"))).hexdigest())')
got_sha=$(curl -fsS --max-time 5 "http://127.0.0.1:$port/health" |
  python3 -I -c 'import json,sys; print(json.load(sys.stdin)["code_sha256"])' 2>/dev/null)
if [[ -n "$got_sha" && "$got_sha" == "$want_sha" ]]; then ok "/health code_sha256 matches the installed runtime"; else bad "/health code_sha256 '$got_sha', want $want_sha"; fi

out=$(run_install)
if [[ "$(field changed "$out")" == nothing* && "$out" != *"install: updated"* ]]; then
  ok "rerun changed nothing"
else
  bad "rerun changed '$(field changed "$out")'"
  printf '%s\n' "$out" | grep 'install: updated' | head -n 5
fi
kill "$pid" 2>/dev/null
for _ in $(seq 50); do kill -0 "$pid" 2>/dev/null || break; sleep 0.1; done
exit "$fail"
