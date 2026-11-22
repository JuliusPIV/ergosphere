#!/bin/bash
# A/B section for issue #63: a session that starts, ends, or a Settings
# restart that lands between the installer's session check and its restart.
# Runs the B tree's tests/install_restart_gate_test.sh once per side, with
# that side's install.sh on the same packages, on that side's port.  Prints
# the section for summary.txt; exit 1 when any case fails on B.
# Needs AB_A_DIR, AB_B_DIR, AB_PORT_A, AB_PORT_B, AB_OUT (see run.sh).
set -u
w="$AB_OUT/restart-gate"
rm -rf "$w"; mkdir -p "$w" || exit 2
log() { printf '%s install_restart_gate.sh: %s\n' "$(date +%T)" "$*" >&2; }
declare -A res
for side in A B; do
  dir=AB_${side}_DIR port=AB_PORT_${side}
  log "$side: ${!dir}/install.sh on port ${!port}"
  ERGOSPHERE_TEST_PORT=${!port} ERGOSPHERE_TEST_INSTALLER="${!dir}/install.sh" TMPDIR="$w" \
    bash "$AB_B_DIR/tests/install_restart_gate_test.sh" >"$w/$side.log" 2>&1
  for c in launch end restart stall; do
    line=$(grep -m1 -E "^(ok|FAIL): +$c:" "$w/$side.log")
    case "$line" in
      ok*) res[$side.$c]="PASS" ;;
      FAIL*) res[$side.$c]="FAIL ${line#*: }" ;;
      *) res[$side.$c]="FAIL not run" ;;
    esac
  done
done

echo
echo "Issue #63: session check vs restart (same packages, each side's install.sh; logs in restart-gate/)"
printf '%-8s %-44s %-44s\n' case A B
bfail=()
for c in launch end restart stall; do
  a=${res[A.$c]} b=${res[B.$c]}
  printf '%-8s %-44s %-44s\n' "$c" "${a:0:44}" "${b:0:44}"
  [[ "$b" == PASS* ]] || bfail+=("$c")
done
if [ ${#bfail[@]} -gt 0 ]; then echo "RESTART GATE FAILURE on B: ${bfail[*]}"; exit 1; fi
exit 0
