#!/usr/bin/env bash
# An upgrade removes the developer files an older installer copied into the
# install dir (#199), and nothing else. The older installer is this
# install.sh without its #97 skip line, which is exactly what it was before
# #97. The package is the checkout packed as a release archive.
#   1. the old installer leaves tests/, docs/, .ghflow*, .git*, install.sh;
#   2. the user adds files, also inside tests/, edits tests/run_all.sh, and
#      links docs/ to a folder outside the install dir;
#      .ghflow.conf is swapped for v0.1.0's copy (an older release's file);
#   3. the current installer removes the archive's developer files, names each
#      one, and leaves the config, the log, the runtime, the server, the
#      user's files, the edited copy, the link and its target alone. install.sh
#      is never removed: a byte-identical copy is what the user would copy in
#      themselves, and bytes cannot tell it from an old installer's (#199 r2-1);
#   4. a second rerun changes nothing;
#   5. with .git in the install dir (a checkout) nothing is removed.
#
# Run: ERGOSPHERE_TEST_PORT=<spare port> bash tests/install_upgrade_dev_files_test.sh
# No network, no git: the package is served from 127.0.0.1 and grok is a stub
# that only answers --version. The server it starts is stopped at the end.
set -u
repo=$(cd "$(dirname "$0")/.." && pwd)
port=${ERGOSPHERE_TEST_PORT:-$(python3 -I "$repo/tests/free_port.py" 3)} || exit 1
work=$(mktemp -d "${TMPDIR:-/tmp}/ergosphere-upgrade-dev-test.XXXXXX")
pids=()
fail=0

# shellcheck disable=SC2317  # run by the EXIT trap
cleanup() {
  local p
  for p in ${pids[@]+"${pids[@]}"}; do kill "$p" 2>/dev/null; done
  for _ in $(seq 100); do
    for p in ${pids[@]+"${pids[@]}"}; do kill -0 "$p" 2>/dev/null && break; p=""; done
    [[ -z "${p:-}" ]] && break
    sleep 0.1
  done
  for p in ${pids[@]+"${pids[@]}"}; do kill -9 "$p" 2>/dev/null; done
  rm -rf -- "$work"
}
trap cleanup EXIT

ok() { echo "ok:   $1"; }
bad() { echo "FAIL: $1"; fail=1; }

mkdir -p "$work/bin" "$work/www" "$work/userhome" "$work/pkg" "$work/outside"
tar -c -C "$repo" --exclude=.git --exclude=__pycache__ --transform 's|^\.|ergosphere-test|' . |
  tar -x -C "$work/pkg" || exit 1
tar -czf "$work/www/src.tar.gz" -C "$work/pkg" ergosphere-test || exit 1
pkg="$work/pkg/ergosphere-test"
devlist=$(cd "$pkg" && find . -type f | sed 's|^\./||' | grep -E '^(tests/|docs/|\.git|\.ghflow|install\.sh$)' | sort)
[[ -n "$devlist" ]] || { bad "package holds no developer files; the test cannot run"; exit 1; }

# The pre-#97 installer: the same script without the skip line and without
# the removal step (#199).
# shellcheck disable=SC2016  # $rel is text to match, not to expand
sed -e '/case "$rel" in tests\/\*|docs\/\*|\.git\*|\.ghflow\*|install\.sh) continue ;; esac/d' \
  -e '/^  remove_dev_files$/d' "$repo/install.sh" >"$work/old-install.sh"
if grep -q 'install.sh) continue ;; esac' "$work/old-install.sh" || grep -qx '  remove_dev_files' "$work/old-install.sh"; then
  bad "could not build the old installer"; exit 1
fi

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
run_install() { # installer-script
  env -u ERGOSPHERE_PORT -u ERGOSPHERE_CONFIG HOME="$work/userhome" PATH="$work/bin:$PATH" TMPDIR="$work" \
    ERGOSPHERE_INSTALL_SOURCE_URL="$url" setsid -w bash "$1" --auto --dir "$app" --port "$port" </dev/null 2>&1
}
field() { sed -n "s/^  $1: *//p" <<<"$2" | head -n 1; }
sha() { sha256sum <"$1" | cut -d' ' -f1; }
devfiles() { (cd "$app" && find . -type f | sed 's|^\./||' | grep -E '^(tests/|docs/|\.git|\.ghflow|install\.sh$)' | sort); }

# 1. The old installer.
out=$(run_install "$work/old-install.sh")
pid=$(field pid "$out")
if [[ -n "$pid" ]]; then pids+=("$pid"); fi
if ! grep -q '^Ergosphere is running' <<<"$out"; then
  bad "old installer did not finish"; printf '%s\n' "$out" | tail -n 15; exit 1
fi
if [[ "$(devfiles)" == "$devlist" ]]; then ok "old installer copied $(wc -l <<<"$devlist") developer files"
else bad "old installer did not copy the developer files; the test cannot run"; exit 1; fi

# 2. The user's own files, and links that point out of the install dir.
printf 'mine\n' >"$app/notes.txt"
printf 'mine\n' >"$app/tests/my-notes.txt"
printf '# my edit\n' >>"$app/tests/run_all.sh"
# An install of an older release holds that release's copy: v0.1.0's .ghflow.conf.
printf '%s\n' 'BASE=main' \
  "TEST_CMD=python3 -B -c 'import ast,sys; [ast.parse(open(f).read()) for f in sys.argv[1:]]' ergosphere.py && bash -n install.sh" \
  >"$app/.ghflow.conf"
mv -- "$app/docs" "$work/outside/docs" && ln -s "$work/outside/docs" "$app/docs"
outside_before=$(cd "$work/outside" && find . -type f -print0 | sort -z | xargs -0 sha256sum)
cfg_sha=$(sha "$app/ergosphere.json")
py_sha=$(sha "$app/ergosphere.py")
log_size=$(stat -c %s "$app/ergosphere.log")

# 3. The current installer.
out=$(run_install "$repo/install.sh")
p=$(field pid "$out")
if [[ -n "$p" && "$p" != "$pid" ]]; then pids+=("$p"); fi
if ! grep -q '^Ergosphere is running' <<<"$out"; then
  bad "upgrade did not finish"; printf '%s\n' "$out" | tail -n 15; exit 1
fi
left=$(devfiles)
want_left=$(printf '%s\n' install.sh tests/my-notes.txt tests/run_all.sh | sort)
if [[ "$left" == "$want_left" ]]; then ok "upgrade removed the developer files; install.sh, the user's tests/my-notes.txt and the edited tests/run_all.sh stay"
else bad "developer files left after the upgrade: $(tr '\n' ' ' <<<"$left")"; fi
named=$(sed -n 's/^install: removed old developer file //p' <<<"$out" | sort)
want_named=$(grep -vE '^(docs/|install\.sh$|tests/run_all\.sh$)' <<<"$devlist")
if [[ "$named" == "$want_named" ]]; then ok "output names each of the $(wc -l <<<"$named") removed files"
else bad "output names '$(head -c 300 <<<"$named")', want each removed file"; fi
if [[ "$(field changed "$out")" == *"old developer files removed"* ]]; then ok "summary lists the removal"
else bad "summary changed: '$(field changed "$out")'"; fi
if [[ ! -e "$app/tests/ab" ]]; then ok "emptied folder tests/ab removed"; else bad "tests/ab is still there"; fi
if [[ -L "$app/docs" && "$(cd "$work/outside" && find . -type f -print0 | sort -z | xargs -0 sha256sum)" == "$outside_before" ]]; then
  ok "link and its target outside the install dir are untouched"
else bad "the docs link or a file outside the install dir changed"; fi
if [[ -f "$app/notes.txt" ]]; then ok "user file notes.txt stays"; else bad "notes.txt was removed"; fi
if [[ "$(sha "$app/ergosphere.json")" == "$cfg_sha" ]]; then ok "config unchanged"; else bad "config changed"; fi
if [[ "$(sha "$app/ergosphere.py")" == "$py_sha" && -f "$app/vendor/xterm.js" ]]; then ok "runtime unchanged"; else bad "runtime changed"; fi
if [[ -f "$app/ergosphere.log" && "$(stat -c %s "$app/ergosphere.log")" -ge "$log_size" ]]; then ok "log kept"; else bad "log removed or truncated"; fi
if [[ "$p" == "$pid" ]]; then ok "server pid $pid kept running"; else bad "server pid '$pid' -> '$p'"; fi

# 4. A second rerun changes nothing.
out=$(run_install "$repo/install.sh")
if [[ "$(field changed "$out")" == nothing* && "$out" != *"install: removed"* ]]; then ok "second rerun changed nothing"
else bad "second rerun changed '$(field changed "$out")'"; fi

# 5. A dir holding .git is a checkout: nothing in it is removed.
mkdir "$app/.git" && cp -p "$pkg/tests/run_all.sh" "$app/tests/run_all.sh"
out=$(run_install "$repo/install.sh")
if [[ -f "$app/tests/run_all.sh" && "$out" == *"holds .git"* && "$out" != *"install: removed"* ]]; then ok "checkout (.git present): nothing removed"
else bad "checkout (.git present): files removed or no note"; fi
exit "$fail"
