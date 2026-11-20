#!/usr/bin/env bash
# Runs every tests/*_test.py and tests/*_test.sh, one at a time, and fails if any fails.
#
# Run: [ERGOSPHERE_TEST_PORT_BASE=<first spare port>] bash tests/run_all.sh
# Each test that reads ERGOSPHERE_TEST_PORT gets its own port: BASE, BASE+1, ...
# when BASE is set, else a free loopback port from tests/free_port.py, with the
# next two free as well (some tests use them).  Each test gets TERM after
# ERGOSPHERE_TEST_TIMEOUT seconds (300) and KILL 10 s later.  No tests found is
# a failure.  A test that prints a line
# starting "SKIP" or "skip:" counts as skipped in the final line.
# Browser tests get NODE_PATH (playwright) and ERGO_CHROME, found as
# tests/ab/run.sh finds them: NODE_PATH, `npm root -g`, /usr/local/lib/*/node_modules;
# ERGO_CHROME, AB_CHROME, then google-chrome, chromium, chromium-browser.
set -u
cd "$(dirname "$0")/.." || exit 1
base=${ERGOSPHERE_TEST_PORT_BASE:-}
limit=${ERGOSPHERE_TEST_TIMEOUT:-300}
next=0 failed=() skipped=() total=0 out=""
trap '[ -z "$out" ] || rm -f "$out"' EXIT

pw_path="${NODE_PATH:-}"
for d in "$(npm root -g 2>/dev/null)" /usr/local/lib/*/node_modules; do
  [ -d "$d/playwright" ] && pw_path="${pw_path:+$pw_path:}$d"
done
[ -n "$pw_path" ] && export NODE_PATH="$pw_path"
if [ -z "${ERGO_CHROME:-}" ]; then
  for c in "${AB_CHROME:-}" google-chrome chromium chromium-browser; do
    [ -n "$c" ] && ERGO_CHROME="$(command -v "$c" 2>/dev/null)" && break
  done
fi
if [ -n "${ERGO_CHROME:-}" ]; then export ERGO_CHROME; else unset ERGO_CHROME; fi
echo "run_all: playwright path: ${pw_path:-none}; browser: ${ERGO_CHROME:-none}"


for t in tests/*_test.py tests/*_test.sh; do
  [ -e "$t" ] || continue
  case "$t" in *.py) run=(python3 -B "$t");; *) run=(bash "$t");; esac
  port_env=()
  if grep -q ERGOSPHERE_TEST_PORT "$t"; then
    if [ -n "$base" ]; then port=$((base + next)); next=$((next + 1)); else port=$(python3 -I tests/free_port.py 3) || exit 1; fi
    port_env=("ERGOSPHERE_TEST_PORT=$port")
  fi
  echo "=== $t${port_env[*]:+ (${port_env[*]})}"
  start=$SECONDS
  # A fresh file per test: a process an earlier test left behind still writes to its own.
  out=$(mktemp) || exit 1
  env ${port_env[@]+"${port_env[@]}"} timeout -k 10 "$limit" "${run[@]}" >"$out"
  rc=$?
  # A file, not a pipe to tee: a server a test leaves behind would hold a pipe open.
  cat "$out"
  total=$((total + 1))
  echo "=== $t rc=$rc $((SECONDS - start))s"
  [ "$rc" -eq 0 ] || failed+=("$t (rc=$rc)")
  grep -Eq '^(SKIP|skip:)' "$out" && skipped+=("$t")
  rm -f "$out"
done

if [ "$total" -eq 0 ]; then echo "FAIL: no tests found in tests/"; exit 1; fi
if [ "${#failed[@]}" -gt 0 ]; then
  echo "FAIL: ${#failed[@]} of $total tests failed:"; printf '  %s\n' "${failed[@]}"; exit 1
fi
if [ "${#skipped[@]}" -gt 0 ]; then
  echo "skipped:"; printf '  %s\n' "${skipped[@]}"
  echo "PASS: $total tests, ${#skipped[@]} skipped"
else
  echo "PASS: all $total tests"
fi
