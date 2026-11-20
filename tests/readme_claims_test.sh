#!/usr/bin/env bash
# Checks README.md and skill/SKILL.md do not state things the app no longer does (#120).
set -u
root="$(dirname "$0")/.."
readme="$(cat "$root/README.md")"
skill="$(cat "$root/skill/SKILL.md")"
fail=0

has()   { if grep -qF -- "$2" <<<"$1"; then echo "ok:   $3"; else echo "FAIL: $3"; fail=1; fi; }
lacks() { if grep -qF -- "$2" <<<"$1"; then echo "FAIL: $3"; fail=1; else echo "ok:   $3"; fi; }

# #99: the README's recompute command for /health code_sha256 must give what the server reports.
recompute='import hashlib as h; print(h.sha256(b"".join(h.sha256(open(f, "rb").read()).digest() for f in ("ergosphere.py", "vendor/addon-fit.js", "vendor/xterm.css", "vendor/xterm.js"))).hexdigest())'
has "$readme" "python3 -c '$recompute'" 'README gives the code_sha256 recompute command'
want="$(cd "$root" && python3 -B -c 'import ergosphere; print(ergosphere.runtime_fingerprint())')"
got="$(cd "$root" && python3 -B -c "$recompute")"
if [ -n "$want" ] && [ "$got" = "$want" ]; then echo "ok:   that command gives the server's code_sha256"; else echo "FAIL: that command gives the server's code_sha256"; fail=1; fi

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
exit "$fail"
