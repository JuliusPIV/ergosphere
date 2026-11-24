#!/bin/bash
# Issue #41: a turn open in events.jsonl reads working with no systemd-inhibit
# child.  Run by run_checks.sh; rows on stdout, exit 1 on a B regression.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
for side in A B; do
  dir=AB_${side}_DIR port=AB_PORT_${side}
  rm -rf "$AB_OUT/turn-$side"; mkdir -p "$AB_OUT/turn-$side"
  printf '%s turn_events.sh: %s: turn events check on %s\n' "$(date +%T)" "$side" "${!port}" >&2
  python3 -B "$HERE/turn_events.py" run "${!dir}" "${!port}" "$AB_OUT/turn-$side" >"$AB_OUT/turn-$side.json" 2>"$AB_OUT/turn-$side.err"
done
python3 -B "$HERE/turn_events.py" summary "$AB_OUT/turn-A.json" "$AB_OUT/turn-B.json"
