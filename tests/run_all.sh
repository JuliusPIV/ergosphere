#!/usr/bin/env bash
# Runs every tests/*_test.py and tests/*_test.sh, one at a time, and fails if any fails.
#
# Run: [ERGOSPHERE_TEST_PORT_BASE=<first spare port>] bash tests/run_all.sh
# Each test that reads ERGOSPHERE_TEST_PORT gets its own port: BASE, BASE+1, ...
# when BASE is set, else a free loopback port from the OS (never 9119-9121,
# 9241 or 9242).  Each test is killed after ERGOSPHERE_TEST_TIMEOUT seconds (300).
set -u
cd "$(dirname "$0")/.." || exit 1
base=${ERGOSPHERE_TEST_PORT_BASE:-}
limit=${ERGOSPHERE_TEST_TIMEOUT:-300}
next=0 failed=() total=0

free_port() {
  python3 -I -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])'
}

for t in tests/*_test.py tests/*_test.sh; do
  [ -e "$t" ] || continue
  case "$t" in *.py) run=(python3 -B "$t");; *) run=(bash "$t");; esac
  port_env=()
  if grep -q ERGOSPHERE_TEST_PORT "$t"; then
    if [ -n "$base" ]; then port=$((base + next)); next=$((next + 1)); else port=$(free_port); fi
    port_env=("ERGOSPHERE_TEST_PORT=$port")
  fi
  echo "=== $t${port_env[*]:+ (${port_env[*]})}"
  start=$SECONDS
  env ${port_env[@]+"${port_env[@]}"} timeout "$limit" "${run[@]}"
  rc=$?
  total=$((total + 1))
  echo "=== $t rc=$rc $((SECONDS - start))s"
  [ "$rc" -eq 0 ] || failed+=("$t (rc=$rc)")
done

if [ "${#failed[@]}" -gt 0 ]; then
  echo "FAIL: ${#failed[@]} of $total tests failed:"; printf '  %s\n' "${failed[@]}"; exit 1
fi
echo "PASS: all $total tests"
