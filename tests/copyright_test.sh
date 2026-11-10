#!/usr/bin/env bash
# Each file's copyright line must name Julius Perkins, not the account handle.
cd "$(dirname "$0")/.." || exit 1
fail=0
for f in README.md ergosphere.py install.sh; do
  if ! grep -q 'Copyright (C) 2026 Julius Perkins' "$f"; then
    echo "FAIL: $f has no copyright line naming Julius Perkins"; fail=1
  fi
  if grep -q 'Copyright.*JuliusPIV' "$f"; then
    echo "FAIL: $f names JuliusPIV as copyright holder"; fail=1
  fi
done
[ "$fail" -eq 0 ] && echo "PASS"
exit "$fail"
