#!/bin/bash
# Issue #132: a stop signal landing while the main thread holds a lock, on each
# side's port again (its server has stopped).  Run by run_checks.sh; rows on
# stdout, exit 1 on a B regression.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
for side in A B; do
  dir=AB_${side}_DIR port=AB_PORT_${side}
  printf '%s sigterm_lock.sh: %s: stop signal check on %s\n' "$(date +%T)" "$side" "${!port}" >&2
  python3 -B "$HERE/sigterm_lock.py" run "${!dir}" "${!port}" >"$AB_OUT/sigterm-$side.json" 2>"$AB_OUT/sigterm-$side.err"
done
python3 -B "$HERE/sigterm_lock.py" summary "$AB_OUT/sigterm-A.json" "$AB_OUT/sigterm-B.json"
