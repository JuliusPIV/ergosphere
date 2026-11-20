#!/usr/bin/env bash
# Checks README.md and skill/SKILL.md do not state things the app no longer does (#120).
set -u
root="$(dirname "$0")/.."
readme="$(cat "$root/README.md")"
skill="$(cat "$root/skill/SKILL.md")"
fail=0

has()   { if grep -qF -- "$2" <<<"$1"; then echo "ok:   $3"; else echo "FAIL: $3"; fail=1; fi; }
lacks() { if grep -qF -- "$2" <<<"$1"; then echo "FAIL: $3"; fail=1; else echo "ok:   $3"; fi; }

# No release so far has a login; a version number in the claim goes stale.
lacks "$readme" 'v0.1'                    'README ties no-login to no version'
lacks "$skill"  'v0.1'                    'SKILL.md ties no-login to no version'
has   "$readme" 'has no login yet'        'README says there is no login yet'
has   "$skill"  'has no login yet'        'SKILL.md says there is no login yet'
# Since #22 the list groups are "Working now", "Idle Sessions" and "Older Sessions".
lacks "$readme" '<b>Active</b>'           'README names no Active group'
lacks "$readme" 'Active, Idle and Older'  'screenshot caption names no Active group'
has   "$readme" '<b>Working now</b>'      'README names the Working now group'
# The button on the session list is "+ New Agent".
has   "$readme" '**+ New Agent**'         'README names the + New Agent button'
lacks "$readme" '| New session |'         'keyboard table names no New session action'
# Since #135 a session held by another Ergosphere server on this machine can be taken over.
lacks "$readme" 'does not attach to sessions run by another Ergosphere server' 'Known limits allows takeover from another server'
# The README spells the approval modes as the + New Agent dialog labels them (#163).
labels="$(sed -n 's/.*name="approval" value="[^"]*"[^>]*> \([^<]*\)<\/label>.*/\1/p' "$root/ergosphere.py")"
[ "$(grep -c . <<<"$labels")" -eq 3 ] || { echo "FAIL: dialog has 3 approval labels"; fail=1; }
while IFS= read -r label; do
  [ -n "$label" ] && has "$readme" "<b>$label</b>" "README names the dialog's '$label' mode"
done <<<"$labels"
for word in '<b>Ask</b>' '<b>Auto</b>' '<b>Always approve</b>' 'Ask is `' 'Auto is `' 'Always approve is `' 'Auto or Always approve'; do
  lacks "$readme" "$word" "README does not spell a mode as $word"
done
exit "$fail"
