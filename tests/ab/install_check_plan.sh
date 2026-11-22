#!/bin/bash
# A/B section for issue #140: does install.sh --check stop where the real run
# stops, and say what the real run does with the server?  Runs the B tree's
# tests/install_check_plan_test.sh once with each side's install.sh (A on
# AB_PORT_A, B on AB_PORT_B) and lines up its checks.  Prints the section for
# summary.txt; exit 1 when any check fails on B or the B-side test exits nonzero.
# Needs AB_A_DIR, AB_B_DIR, AB_PORT_A, AB_PORT_B, AB_OUT (see run.sh).
# B's exit status counts too: a timeout or crash after the last ok line leaves
# no FAIL line, so the name check alone would pass it.
set -u
w="$AB_OUT/check-plan"
rm -rf "$w"; mkdir -p "$w" || exit 2
log() { printf '%s install_check_plan.sh: %s\n' "$(date +%T)" "$*" >&2; }
test="$AB_B_DIR/tests/install_check_plan_test.sh"
b_rc=0

for side in A B; do
  dir=AB_${side}_DIR port=AB_PORT_${side}
  log "$side: $test with ${!dir}/install.sh on ${!port}"
  rc=0
  ERGOSPHERE_TEST_PORT=${!port} ERGOSPHERE_TEST_INSTALLER="${!dir}/install.sh" timeout 300 \
    bash "$test" >"$w/$side.log" 2>&1 || rc=$?
  log "$side: exit $rc"
  [ "$side" = B ] && b_rc=$rc
done

# Check names are the test's ok/FAIL lines up to the first ':' after the
# prefix, with pids and ports dropped so both sides line up.
names() { sed -n 's/^\(ok\|FAIL\): *//p' "$1" | sed -E 's/: .*//; s/[0-9]{3,}/N/g'; }
echo
echo "Issue #140: install.sh --check against the real run (same test, each side's install.sh)"
printf '%-58s %-5s %-5s\n' check A B
regress=() bfail=() n=0
declare -A seen
while IFS= read -r c; do
  [[ -n "${seen[$c]:-}" ]] && continue
  seen[$c]=1 n=$((n + 1))
  a=FAIL b=FAIL
  sed -n 's/^ok: *//p' "$w/A.log" | sed -E 's/: .*//; s/[0-9]{3,}/N/g' | grep -qxF -- "$c" && a=PASS
  sed -n 's/^ok: *//p' "$w/B.log" | sed -E 's/: .*//; s/[0-9]{3,}/N/g' | grep -qxF -- "$c" && b=PASS
  printf '%-58s %-5s %-5s\n' "${c:0:58}" "$a" "$b"
  if [[ "$a" == PASS && "$b" != PASS ]]; then regress+=("$c"); fi
  if [[ "$b" != PASS ]]; then bfail+=("$c"); fi
done < <(names "$w/B.log"; names "$w/A.log")
if [ "$n" -eq 0 ]; then echo "RUN BROKE: the test printed no checks (see $w)"; exit 1; fi
if [ "$b_rc" -ne 0 ]; then bfail+=("test exited $b_rc on B (see $w/B.log)"); fi
if [ ${#regress[@]} -gt 0 ]; then echo "REGRESSION: passes on A, fails on B: ${regress[*]}"; fi
if [ ${#bfail[@]} -gt 0 ]; then echo "CHECK FAILURE on B: ${bfail[*]}"; exit 1; fi
exit 0
