"""The tab that presses Quit gets the ended frame (issue #212).

Runs tests/ab/quit_ended.py on this checkout: the quitting tab's sender is
held until `_term` has detached it, so the ended frame is still queued when
detach runs.  Also checks a second tab, a late attach, a sender stuck on a
slow socket and a server stop on the same exit path.

Run: python3 -B tests/quit_ended_test.py
Stdlib only, no network, no real grok.
"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    proc = subprocess.run(
        [sys.executable, "-B", str(ROOT / "tests" / "ab" / "quit_ended.py"), "run", str(ROOT)],
        capture_output=True, text=True, timeout=240,
    )
    try:
        checks = json.loads(proc.stdout)["checks"]
    except (ValueError, KeyError):
        print("FAIL: quit_ended.py rc=%s: %s %s" % (proc.returncode, proc.stdout[-500:], proc.stderr[-1500:]))
        return 1
    failed = [n for n, c in checks.items() if not c["ok"]]
    for name, check in checks.items():
        print("%s: %s: %s" % ("PASS" if check["ok"] else "FAIL", name, check["value"]))
    if failed:
        print("FAIL: %s" % ", ".join(failed))
        return 1
    print("PASS: the ended frame reaches the quitting tab; second tab, late attach, slow socket, server stop")
    return 0


if __name__ == "__main__":
    sys.exit(main())
