#!/usr/bin/env bash
# tests/ab/held_quit.py summary must honor AB_NO_BROWSER=1 when browser checks
# could not run (no node, playwright or Chrome), as run.sh's browser gate does.
# Without the flag it must still fail; a check that ran and failed still fails.
set -u
repo=$(cd "$(dirname "$0")/.." && pwd)
work=$(mktemp -d "${TMPDIR:-/tmp}/ab-held-summary.XXXXXX") || exit 2
trap 'rm -rf -- "$work"' EXIT
printf '%s\n' '{"unavailable": "no node or Chrome"}' >"$work/A.json"
printf '%s\n' '{"unavailable": "no node or Chrome"}' >"$work/B.json"

env -u AB_NO_BROWSER python3 -B "$repo/tests/ab/held_quit.py" summary "$work/A.json" "$work/B.json" >/dev/null 2>&1
rc_unset=$?
AB_NO_BROWSER=1 python3 -B "$repo/tests/ab/held_quit.py" summary "$work/A.json" "$work/B.json" >/dev/null 2>&1
rc_allowed=$?
if [ "$rc_unset" -eq 0 ]; then
  echo "FAIL: held summary passed without AB_NO_BROWSER when browser checks did not run"
  exit 1
fi
if [ "$rc_allowed" -ne 0 ]; then
  echo "FAIL: held summary rc=$rc_allowed with AB_NO_BROWSER=1 when browser checks did not run"
  exit 1
fi
echo "ok: held summary honors AB_NO_BROWSER=1 for unavailable browser checks"
