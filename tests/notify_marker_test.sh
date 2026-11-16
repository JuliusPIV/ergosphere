#!/usr/bin/env bash
# Deploy tooling turns on Type=notify and the watchdog only when ergosphere.py
# has this top-level marker line; code that sends READY=1 must define it.
set -u
cd "$(dirname "$0")/.." || exit 1
fail=0
if grep -qE '^SYSTEMD_NOTIFY = 1( |$)' ergosphere.py; then
  echo "ok:   SYSTEMD_NOTIFY marker line present"
else
  echo "FAIL: ergosphere.py has no '^SYSTEMD_NOTIFY = 1' marker line"; fail=1
fi
if grep -q 'sd_notify("READY=1")' ergosphere.py; then
  echo "ok:   ergosphere.py sends READY=1"
else
  echo "FAIL: ergosphere.py does not send READY=1, so the marker would lie"; fail=1
fi
exit "$fail"
