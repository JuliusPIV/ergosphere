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
# Phones (#121): touch scroll with fling (#66), pinch text size 8-28 px (#86),
# keyboard refit (#137); the scroll bar drag (#136) is mouse only; #158 is open.
has   "$readme" '<b>Works on phones</b>'  'Features has a Works on phones row'
has   "$readme" 'Drag one finger on the terminal to scroll grok' 'README says a one-finger drag scrolls grok'
has   "$readme" 'from 8 to 28 px'         'README gives the pinch text size range'
has   "$readme" "so grok's input line stays in view" 'README says the keyboard does not hide the input line'
has   "$readme" "Dragging grok's scroll bar takes a mouse" 'README says the scroll bar drag is mouse only'
has   "$readme" 'issues/158'              'Known limits lists the page pinch resize (#158)'
# The button on the session list is "+ New Agent".
has   "$readme" '**+ New Agent**'         'README names the + New Agent button'
lacks "$readme" '| New session |'         'keyboard table names no New session action'
# Since #135 a session held by another Ergosphere server on this machine can be taken over.
lacks "$readme" 'does not attach to sessions run by another Ergosphere server' 'Known limits allows takeover from another server'
# #115: the installer refuses a fresh install dir that already holds the ERGOSPHERE_CONFIG file.
has   "$readme" 'Point it at a file outside the install directory, for example' 'README says to keep ERGOSPHERE_CONFIG outside the install dir'
has   "$readme" 'is refused on purpose'  'README says a non-Ergosphere non-empty install dir is refused on purpose'
exit "$fail"
