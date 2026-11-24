"""tests/ab/ab.py summary: B must report every HTTP check in ORDER (#179).

Both A/B sides run B's ab.py, so an HTTP check dropped from it is missing on
both sides and the row simply disappears.  Runs `ab.py summary` on side folders
written here (no server, no browser): one where both sides lack session_list
must exit 1 and name it; a complete one must exit 0.

Run: python3 -B tests/ab_http_checks_test.py
"""
import json
import os
import subprocess
import sys
import tempfile

AB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ab", "ab.py")


def write_side(root, label, names):
    d = os.path.join(root, label)
    os.makedirs(d)
    checks = {n: {"ok": True, "value": "ok"} for n in names}
    with open(os.path.join(d, "http.json"), "w", encoding="utf-8") as fh:
        json.dump({"checks": checks, "info": {"code_sha256": "x"}}, fh)
    # run.sh writes vendor_listed (also in HTTP_CHECKS) to its own file.
    with open(os.path.join(d, "vendor.json"), "w", encoding="utf-8") as fh:
        json.dump({"checks": {"vendor_listed": {"ok": True, "value": "ok"}}}, fh)
    # No browser on the host: only a warning, so the HTTP rule decides the exit.
    with open(os.path.join(d, "browser.json"), "w", encoding="utf-8") as fh:
        json.dump({"unavailable": "test"}, fh)
    return d


def run_summary(names):
    with tempfile.TemporaryDirectory() as root:
        a = write_side(root, "A", names)
        b = write_side(root, "B", names)
        p = subprocess.run([sys.executable, "-B", AB, "summary", a, b],
                           capture_output=True, text=True, timeout=60)
        return p.returncode, p.stdout + p.stderr


def main():
    fail = 0
    rc, out = run_summary(["health", "open_session"])
    if rc != 1 or "B did not report HTTP check(s): session_list" not in out:
        print("FAIL: session_list dropped on both sides: rc=%s, want 1 naming it:\n%s" % (rc, out))
        fail = 1
    rc, out = run_summary(["health", "session_list", "open_session"])
    if rc != 0:
        print("FAIL: every HTTP check reported: rc=%s, want 0:\n%s" % (rc, out))
        fail = 1
    if fail:
        return 1
    print("PASS: a dropped HTTP check fails the A/B; a complete run passes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
