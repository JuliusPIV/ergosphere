#!/usr/bin/env bash
# tests/ab/install_check_plan.sh must fail the A/B gate when the B-side test
# exits nonzero, even after it printed every ok line (a timeout or a crash
# after the last check leaves no FAIL line to catch).  Stubs stand in for both
# sides; the B stub exits 124, what timeout reports, with no FAIL line.
set -u
repo=$(cd "$(dirname "$0")/.." && pwd)
work=$(mktemp -d "${TMPDIR:-/tmp}/ab-check-plan-exit.XXXXXX") || exit 2
trap 'rm -rf -- "$work"' EXIT
mkdir -p "$work/A/tests" "$work/B/tests" "$work/out" || exit 2
: >"$work/A/install.sh" && : >"$work/B/install.sh" || exit 2

cat >"$work/stub.sh" <<'EOF'
#!/bin/bash
echo "ok: plan prints server action"
echo "ok: stop point matches real run"
case "$ERGOSPHERE_TEST_INSTALLER" in
  */B/*) exit 124 ;;
esac
exit 0
EOF
cp "$work/stub.sh" "$work/A/tests/install_check_plan_test.sh" || exit 2
cp "$work/stub.sh" "$work/B/tests/install_check_plan_test.sh" || exit 2

AB_A_DIR="$work/A" AB_B_DIR="$work/B" AB_PORT_A=1 AB_PORT_B=2 AB_OUT="$work/out" \
  bash "$repo/tests/ab/install_check_plan.sh" >"$work/run.txt" 2>&1
rc=$?
if [ "$rc" -eq 0 ] || ! grep -q 'CHECK FAILURE on B' "$work/run.txt"; then
  echo "FAIL: A/B check passed (rc=$rc) with the B-side test exiting 124"
  cat "$work/run.txt"
  exit 1
fi
echo "ok: B-side test exit 124 fails the A/B check (rc=$rc)"
