#!/bin/bash
# Issue #218: the PTY master is closed once (in process, forced close orders).
# Run by run_checks.sh; rows on stdout, exit 1 when a check fails on B.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
for side in A B; do
  dir=AB_${side}_DIR
  printf '%s master_close.sh: %s: master close check\n' "$(date +%T)" "$side" >&2
  python3 -B "$HERE/master_close.py" run "${!dir}" >"$AB_OUT/mclose-$side.json" 2>"$AB_OUT/mclose-$side.err"
done
python3 -B "$HERE/master_close.py" summary "$AB_OUT/mclose-A.json" "$AB_OUT/mclose-B.json"
