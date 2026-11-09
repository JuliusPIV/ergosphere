#!/usr/bin/env bash
# Checks the README roadmap, section by section.
set -u
readme="$(dirname "$0")/../README.md"

# Print the lines under one "### " heading, up to the next heading.
section() {
  awk -v h="$1" '
    /^##+ / { inside = ($0 == h); next }
    inside { print }
  ' "$readme"
}

planned="$(section '### 🗓️ Planned')"
exploring="$(section '### 🔭 Exploring')"
fail=0

has()   { if grep -qF -- "$2" <<<"$1"; then echo "ok:   $3"; else echo "FAIL: $3"; fail=1; fi; }
lacks() { if grep -qF -- "$2" <<<"$1"; then echo "FAIL: $3"; fail=1; else echo "ok:   $3"; fi; }

has   "$exploring" '**Turn notifications**' 'Turn notifications under Exploring'
lacks "$planned"   '**Turn notifications**' 'Turn notifications not under Planned'
has   "$exploring" '**macOS support**'      'macOS support under Exploring'
lacks "$planned"   '**macOS support**'      'macOS support not under Planned'
lacks "$(cat "$readme")" 'One theme'        'One theme is gone'
has   "$planned"   '**Consistent theming**' 'Consistent theming under Planned'
has   "$exploring" '**More themes**'        'More themes under Exploring'
exit "$fail"
