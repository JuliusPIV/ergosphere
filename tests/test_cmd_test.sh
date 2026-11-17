#!/usr/bin/env bash
# The review gate's TEST_CMD (.ghflow.conf) runs the tests in tests/: a tree
# whose only test fails must fail TEST_CMD.  Works on a throwaway copy.
#
# Run: bash tests/test_cmd_test.sh
set -u
cd "$(dirname "$0")/.." || exit 1
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
cp ergosphere.py install.sh .ghflow.conf "$tmp/"
mkdir "$tmp/tests"
cp tests/run_all.sh "$tmp/tests/"
printf 'echo "deliberately broken"; exit 1\n' >"$tmp/tests/broken_test.sh"
cmd=$(sed -n 's/^TEST_CMD=//p' .ghflow.conf)
if [ -z "$cmd" ]; then echo "FAIL: no TEST_CMD in .ghflow.conf"; exit 1; fi
if (cd "$tmp" && bash -c "$cmd") >"$tmp/out" 2>&1; then
  echo "FAIL: TEST_CMD passed with a failing test in tests/"; exit 1
fi
if ! grep -q 'deliberately broken' "$tmp/out"; then
  echo "FAIL: TEST_CMD failed but never ran tests/broken_test.sh:"; tail -5 "$tmp/out"; exit 1
fi
echo "PASS: TEST_CMD runs the tests in tests/ and fails when one fails"
