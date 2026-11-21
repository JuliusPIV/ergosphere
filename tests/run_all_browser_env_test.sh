#!/usr/bin/env bash
# tests/run_all.sh with NODE_PATH, AB_CHROME and ERGO_CHROME unset (#157):
# a test that prints SKIP is counted in the final line, and when the host has
# playwright and Chrome where tests/ab/run.sh looks, run_all hands both to the
# tests, so the browser tests run instead of skipping.  Works on a throwaway copy.
#
# Run: bash tests/run_all_browser_env_test.sh
set -u
cd "$(dirname "$0")/.." || exit 1
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir "$tmp/tests"
cp tests/run_all.sh "$tmp/tests/"
printf 'echo "SKIP stand-in: nothing to check"\n' >"$tmp/tests/a_skip_test.sh"
cat >"$tmp/tests/b_env_test.sh" <<'EOF'
echo "NODE_PATH=${NODE_PATH:-} ERGO_CHROME=${ERGO_CHROME:-}"
if [ -n "${ERGO_CHROME:-}" ] && [ -x "$ERGO_CHROME" ] && node -e 'require("playwright")' >/dev/null 2>&1; then
  echo "browser env ok"
fi
EOF
run() { (cd "$tmp" && env -u NODE_PATH -u AB_CHROME -u ERGO_CHROME bash tests/run_all.sh) >"$tmp/out" 2>&1; }
run || { echo "FAIL: run_all failed:"; tail -5 "$tmp/out"; exit 1; }
last=$(tail -n 1 "$tmp/out")
fail=0
if [ "$last" != "PASS: 2 tests, 1 skipped" ]; then
  echo "FAIL: final line '$last', want 'PASS: 2 tests, 1 skipped'"; fail=1
fi

pw=""
for d in "$(npm root -g 2>/dev/null)" /usr/local/lib/*/node_modules; do
  [ -d "$d/playwright" ] && pw=$d && break
done
chrome=""
for c in google-chrome chromium chromium-browser; do chrome=$(command -v "$c") && break; done
if [ -z "$pw" ] || [ -z "$chrome" ] || ! command -v node >/dev/null; then
  echo "skip: no node, playwright or Chrome on this host; the hand-off check needs them"
  exit "$fail"
fi
if ! grep -q '^browser env ok$' "$tmp/out"; then
  echo "FAIL: playwright is in $pw and Chrome at $chrome, but run_all did not hand them to the tests:"
  grep '^NODE_PATH=' "$tmp/out"; fail=1
fi
[ "$fail" -eq 0 ] || exit 1
echo "PASS: run_all counts skips and hands playwright ($pw) and Chrome ($chrome) to the tests"
