#!/bin/bash
# Runs every A/B check script (#243): bash run_checks.sh <checks-dir> [<base-checks-dir>]
# run.sh calls it after its own sections, with stdout going to summary.txt.
# Each <checks-dir>/*.sh runs with bash, sorted by name, one at a time, with
# run.sh's environment (AB_A_DIR, AB_B_DIR, AB_PORT_A, AB_PORT_B, AB_OUT, ...;
# both servers have stopped, so the ports are free again).  A script prints its
# summary rows on stdout and exits 0 when B passes; its output is kept in
# $AB_OUT/checks/<name>.out and .err.  Exit 1 when a script exits non-zero,
# is killed by a signal, prints no row, or when a script in <base-checks-dir>
# (the base tree's checks) is missing here: both sides run this tree's harness,
# so a deleted check would otherwise just vanish.
set -u
dir="$1" base="${2:-}"
[ -n "${AB_OUT:-}" ] || { echo "run_checks.sh: AB_OUT is not set" >&2; exit 2; }
mkdir -p "$AB_OUT/checks" || exit 2
log() { printf '%s run_checks.sh: %s\n' "$(date +%T)" "$*" >&2; }
names() { [ -d "$1" ] && find "$1" -maxdepth 1 -type f -name '*.sh' -printf '%f\n' | LC_ALL=C sort; }
rc=0
while IFS= read -r f; do
  [ -n "$f" ] || continue
  name="${f%.sh}" o="$AB_OUT/checks/${f%.sh}"
  log "$name: start"
  src=0
  # Files, not a pipe: a server a check leaves behind would hold a pipe open.
  bash "$dir/$f" </dev/null >"$o.out" 2>"$o.err" || src=$?
  log "$name: exit $src"
  printf '\n[checks/%s]\n' "$name"
  cat "$o.out"
  if [ "$src" -gt 128 ]; then
    echo "CHECK FAILURE: checks/$name was killed by signal $((src - 128)) (see checks/$name.err)"; rc=1
  elif [ "$src" -ne 0 ]; then
    echo "CHECK FAILURE: checks/$name exited $src (see checks/$name.err)"; rc=1
  elif ! grep -q '[^[:space:]]' "$o.out"; then
    echo "CHECK FAILURE: checks/$name printed no row"; rc=1
  fi
done < <(names "$dir")
if [ -n "$base" ]; then
  missing=$(LC_ALL=C comm -23 <(names "$base") <(names "$dir") | tr '\n' ' ')
  if [ -n "$missing" ]; then
    echo "CHECK FAILURE: check script(s) in the base tree are missing here: $missing"; rc=1
  fi
fi
exit "$rc"
