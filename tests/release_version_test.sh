#!/usr/bin/env bash
# A release needs no edits: no release tag is hard-coded in the installer or the
# README install commands, the installer finds the latest release itself (or
# installs --version vA.B.C), and the reported version comes from the release
# archive (git export-subst), with a plain checkout reporting "dev".
set -u
cd "$(dirname "$0")/.." || exit 1
root=$PWD
fail=0
ok()  { echo "ok:   $1"; }
bad() { echo "FAIL: $1"; fail=1; }
tmp=$(mktemp -d)
stub_pid=""
trap '[ -n "$stub_pid" ] && kill "$stub_pid" 2>/dev/null; rm -rf "$tmp"' EXIT

# 1. No hard-coded release tag in install.sh or in any install command.
if grep -nE 'v[0-9]+\.[0-9]+\.[0-9]+' install.sh; then bad "install.sh names a release tag"; else ok "install.sh names no release tag"; fi
cmds=$(grep -h 'install.sh' README.md skill/SKILL.md | grep 'curl ')
if grep -vF 'https://raw.githubusercontent.com/JuliusPIV/ergosphere/main/install.sh' <<<"$cmds"; then
  bad "an install command does not use the main install.sh link"
else ok "every install command uses the main install.sh link"; fi
if grep -q 'badge/version-v' README.md; then bad "README version badge is hard-coded"; else ok "README badge is not hard-coded"; fi

# 2. Version: a checkout says dev; a git archive (what GitHub serves for a tag) says the tag.
ver_of() { python3 -I -c 'import runpy,sys; print(runpy.run_path(sys.argv[1])["VERSION"])' "$1"; }
v=$(ver_of ergosphere.py)
if [ "$v" = dev ]; then ok "checkout reports dev"; else bad "checkout reports '$v', expected dev"; fi
mkdir "$tmp/arc"
git archive --format=tar HEAD ergosphere.py | tar -x -C "$tmp/arc"
want=$(git describe --tags --match 'v[0-9]*' HEAD 2>/dev/null)
v=$(ver_of "$tmp/arc/ergosphere.py")
if [ -z "$want" ]; then echo "skip: no release tags in this clone; archive reports '$v'"
elif [ "$v" = "${want#v}" ]; then ok "archive reports $v (git describe $want)"
else bad "archive reports '$v', expected '${want#v}'"; fi

# 3. The installer: latest-release lookup and --version, in --check mode (changes nothing).
mkdir -p "$tmp/bin"
printf '#!/bin/sh\necho "grok 0.0.0-test"\n' >"$tmp/bin/grok"; chmod +x "$tmp/bin/grok"
python3 -I -c '
import http.server, sys
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(302)
        self.send_header("Location", "https://github.com/JuliusPIV/ergosphere/releases/tag/v9.8.7")
        self.end_headers()
    do_HEAD = do_GET
    def log_message(self, *a): pass
s = http.server.HTTPServer(("127.0.0.1", 0), H)
print(s.server_address[1], flush=True)
s.serve_forever()' >"$tmp/port" &
stub_pid=$!
for _ in $(seq 50); do [ -s "$tmp/port" ] && break; sleep 0.1; done
latest="http://127.0.0.1:$(cat "$tmp/port")/releases/latest"
run() {
  env -u ERGOSPHERE_PORT -u ERGOSPHERE_INSTALL_SOURCE_URL -u ERGOSPHERE_TOKEN PATH="$tmp/bin:$PATH" \
    ERGOSPHERE_INSTALL_LATEST_URL="$latest" bash "$root/install.sh" --check --dir "$tmp/dest" --port 9560 "$@" 2>&1 </dev/null
}
out=$(run)
if grep -q 'release  *v9\.8\.7 (latest)' <<<"$out"; then ok "lookup finds the latest release"; else bad "lookup: $out"; fi
out=$(run --version v1.2.3)
if grep -q 'release  *v1\.2\.3 (--version)' <<<"$out"; then ok "--version v1.2.3 is used"; else bad "--version: $out"; fi
if run --version 1.2 >/dev/null; then bad "--version 1.2 was accepted"; else ok "--version 1.2 is refused"; fi
kill "$stub_pid" 2>/dev/null; stub_pid=""
latest="http://127.0.0.1:9/none"
if out=$(run); then bad "failed lookup did not block: $out"
elif grep -q 'release' <<<"$out"; then ok "a failed lookup blocks"; else bad "failed lookup: $out"; fi
[ -d "$tmp/dest" ] && bad "--check created the install dir"

[ "$fail" -eq 0 ] && echo PASS
exit "$fail"
