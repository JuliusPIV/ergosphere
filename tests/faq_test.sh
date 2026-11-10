#!/usr/bin/env bash
# Checks the README FAQ explains grok's 500 retry message and Edge's writing-hint dot.
set -u
readme="$(dirname "$0")/../README.md"

# Print the lines under one "## " heading, up to the next "## " heading.
section() {
  awk -v h="$1" '
    /^## / { inside = ($0 == h); next }
    inside { print }
  ' "$readme"
}

faq="$(section '## ❓ FAQ')"
fail=0

has() { if grep -qF -- "$2" <<<"$1"; then echo "ok:   $3"; else echo "FAIL: $3"; fail=1; fi; }

has "$faq" '"Server error (500) | Retrying (attempt N)"' '500 retry entry in FAQ'
has "$faq" 'blue dot'                                    'Edge blue dot entry in FAQ'
has "$faq" 'Show writing hints'                          'Edge entry names the setting'
exit "$fail"
