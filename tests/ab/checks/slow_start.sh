#!/bin/bash
# Issue #45: one session's hung start, on each side's port again (its server
# has stopped).  Run by run_checks.sh; rows on stdout, exit 1 on a B regression.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
for side in A B; do
  dir=AB_${side}_DIR port=AB_PORT_${side}
  rm -rf "$AB_OUT/slow-$side"; mkdir -p "$AB_OUT/slow-$side"
  printf '%s slow_start.sh: %s: slow start check on %s\n' "$(date +%T)" "$side" "${!port}" >&2
  python3 -B "$HERE/slow_start.py" run "${!dir}" "${!port}" "$AB_OUT/slow-$side" >"$AB_OUT/slow-$side.json" 2>"$AB_OUT/slow-$side.err"
done
python3 -B "$HERE/slow_start.py" summary "$AB_OUT/slow-A.json" "$AB_OUT/slow-B.json"
