#!/usr/bin/env bash
# tests/run_all.sh --require-browser (#147): with ERGO_CHROME at a missing path (the browser tests then skip), run_all must
# fail instead of letting the browser tests skip; without the flag it still
# passes, so a box with no browser can run the tests.  Works on a throwaway copy
# with a PATH that holds only the tools run_all needs.  With ERGO_CHROME unset the
# browser tests fall back to /usr/bin/google-chrome, so run_all must count that as present.
#
# Run: bash tests/run_all_require_browser_test.sh
set -u
cd "$(dirname "$0")/.." || exit 1
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir "$tmp/tests" "$tmp/bin"
cp tests/run_all.sh "$tmp/tests/"
printf 'echo "stand-in ran"\n' >"$tmp/tests/a_test.sh"
for c in bash env timeout mktemp cat grep rm dirname python3 node npm; do
  p=$(command -v "$c") && ln -s "$p" "$tmp/bin/$c"
done
run() { # flag...
  (cd "$tmp" && env -u NODE_PATH ERGO_CHROME=/nonexistent/chrome PATH="$tmp/bin" \
    bash tests/run_all.sh "$@") >"$tmp/out" 2>&1
}
fail=0
if ! run; then
  echo "FAIL: without --require-browser, run_all failed with Chrome hidden:"; tail -5 "$tmp/out"; fail=1
fi
if run --require-browser; then
  echo "FAIL: run_all --require-browser passed with Chrome hidden:"; tail -5 "$tmp/out"; fail=1
elif ! grep -q '^stand-in ran$' "$tmp/out" || ! grep -q '^FAIL: --require-browser' "$tmp/out"; then
  echo "FAIL: run_all --require-browser failed, but did not run the tests and then name the missing browser:"
  tail -5 "$tmp/out"; fail=1
fi
# The review gate's TEST_CMD must pass the flag (its sandbox clears the
# environment, so the flag has to be in the command itself).
cp ergosphere.py install.sh .ghflow.conf "$tmp/"
cmd=$(sed -n 's/^TEST_CMD=//p' .ghflow.conf)
if (cd "$tmp" && env -u NODE_PATH ERGO_CHROME=/nonexistent/chrome PATH="$tmp/bin" \
    bash -c "$cmd") >"$tmp/out" 2>&1; then
  echo "FAIL: the review gate's TEST_CMD passed with Chrome hidden (browser tests would skip):"
  tail -3 "$tmp/out"; fail=1
fi
# No Chrome on PATH and no ERGO_CHROME/AB_CHROME: the browser tests use
# /usr/bin/google-chrome, so on a host that has it the gate must pass.
if [ -x /usr/bin/google-chrome ] && (cd "$tmp" && env -u NODE_PATH -u ERGO_CHROME -u AB_CHROME \
    PATH="$tmp/bin" bash tests/run_all.sh) 2>&1 | grep -q '^run_all: playwright path: /'; then
  if ! (cd "$tmp" && env -u NODE_PATH -u ERGO_CHROME -u AB_CHROME PATH="$tmp/bin" \
      bash tests/run_all.sh --require-browser) >"$tmp/out" 2>&1; then
    echo "FAIL: --require-browser failed although the browser tests would use /usr/bin/google-chrome:"
    tail -3 "$tmp/out"; fail=1
  fi
else
  echo "note: no /usr/bin/google-chrome or playwright here; fallback case not checked"
fi
[ "$fail" -eq 0 ] || exit 1
echo "PASS: --require-browser and TEST_CMD fail with Chrome hidden; plain run_all still passes"
