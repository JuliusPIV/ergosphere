#!/usr/bin/env bash
# tests/run_all.sh captures each test's output on its own: a background writer
# that an earlier test leaves running must not land in the next test's output
# or mark it skipped.  The writer waits until the next test has started, writes,
# and signals; the next test waits for that signal (10 s deadline), so the
# overlap happens on every run.  Works on a throwaway copy.
#
# Run: bash tests/run_all_capture_test.sh
set -u
cd "$(dirname "$0")/.." || exit 1
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir "$tmp/tests" "$tmp/sync"
cp tests/run_all.sh "$tmp/tests/"
cat >"$tmp/tests/a_leaves_writer_test.sh" <<'EOF'
( for _ in $(seq 200); do [ -e sync/b_started ] && break; sleep 0.05; done
  echo "SKIP stray line from a_leaves_writer_test"; touch sync/written ) &
echo "a done"
EOF
cat >"$tmp/tests/b_next_test.sh" <<'EOF'
touch sync/b_started
for _ in $(seq 200); do [ -e sync/written ] && break; sleep 0.05; done
[ -e sync/written ] || { echo "stray writer never wrote"; exit 1; }
echo "b done"
EOF
(cd "$tmp" && bash tests/run_all.sh) >"$tmp/out" 2>&1 || { echo "FAIL: run_all failed:"; tail -5 "$tmp/out"; exit 1; }
b_out=$(sed -n '/^=== tests\/b_next_test.sh$/,/^=== tests\/b_next_test.sh rc=/p' "$tmp/out")
fail=0
if grep -q 'stray line' <<<"$b_out"; then
  echo "FAIL: the stray writer's line shows in the next test's output:"; echo "$b_out"; fail=1
fi
last=$(tail -n 1 "$tmp/out")
if [ "$last" != "PASS: all 2 tests" ]; then
  echo "FAIL: final line '$last', want 'PASS: all 2 tests'"; fail=1
fi
[ "$fail" -eq 0 ] || exit 1
echo "PASS: a writer left by one test does not reach the next test's output"
