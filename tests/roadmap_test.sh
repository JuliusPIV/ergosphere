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

# Every row links its issue(s) in the last cell, as [#N](.../issues/N).
# The one row the owner has not filed an issue for says so instead.
next_up="$(section '### 🔜 Next up')"
roadmap="$(printf '%s\n' "$next_up" "$planned" "$exploring")"
link='\[#([0-9]+)\]\(https://github\.com/JuliusPIV/ergosphere/issues/\1\)'
while IFS= read -r row; do
  name="${row#| }"; name="${name%% | *}"
  cell="${row% |}"; cell="${cell##*| }"
  if [ "$name" = '**Sessions survive Ergosphere updates**' ] && [ "$cell" = 'No issue yet' ]; then
    echo "ok:   $name has no issue yet"
  elif ! grep -qvE "^$link\$" <<<"${cell//, /$'\n'}"; then
    echo "ok:   $name links an issue"
  else
    echo "FAIL: $name links no issue (last cell: $cell)"; fail=1
  fi
done < <(grep '^| \*\*' <<<"$roadmap")

# Closed issues are off the roadmap: #54 (not planned), #82 (shipped).
lacks "$roadmap" '**Resume on click, as a setting**' 'Resume on click (#54, closed) is gone'
lacks "$roadmap" '**More installer port presets**'   'Installer port presets (#82, closed) are gone'
exit "$fail"
