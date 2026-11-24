"""tests/ab/ab.py summary: a check B reports as null is missing, not "not run" (#234).

Both A/B sides run B's checks, so a check B reports as null (or as anything that
is not an object with an `ok` field) is null on both sides: the row shows FAIL
on both, which is no regression, and the A/B used to exit 0.  Runs `ab.py
summary` on side folders written here (no server, no browser): a browser check
or an HTTP check that is null, a string, or an object without `ok` on both
sides must exit 1 and name it; a complete run must exit 0.

Run: python3 -B tests/ab_null_checks_test.py
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
AB = os.path.join(HERE, "ab", "ab.py")
sys.path.insert(0, os.path.join(HERE, "ab"))
from ab import HTTP_CHECKS, ORDER  # noqa: E402

GOOD = {"ok": True, "value": "ok"}


def write_side(root, label, http, browser):
    d = os.path.join(root, label)
    os.makedirs(d)
    # As run.sh writes them: vendor_listed in vendor.json, the rest in http.json.
    http = dict(http)
    with open(os.path.join(d, "vendor.json"), "w", encoding="utf-8") as fh:
        json.dump({"checks": {"vendor_listed": http.pop("vendor_listed")}}, fh)
    with open(os.path.join(d, "http.json"), "w", encoding="utf-8") as fh:
        json.dump({"checks": http, "info": {"code_sha256": "x"}}, fh)
    with open(os.path.join(d, "browser.json"), "w", encoding="utf-8") as fh:
        json.dump({"browser": "x", "checks": browser}, fh)
    return d


def run_summary(name=None, value=None):
    http = {n: GOOD for n in HTTP_CHECKS}
    browser = {n: GOOD for n in ORDER if n not in HTTP_CHECKS}
    if name is not None:
        (http if name in HTTP_CHECKS else browser)[name] = value
    with tempfile.TemporaryDirectory() as root:
        a = write_side(root, "A", http, browser)
        b = write_side(root, "B", http, browser)
        p = subprocess.run([sys.executable, "-B", AB, "summary", a, b],
                           capture_output=True, text=True, timeout=60)
        return p.returncode, p.stdout + p.stderr


def main():
    fail = 0
    cases = [("wheel_scroll", None, "browser"), ("wheel_scroll", "x", "browser"),
             ("resume_box", {"value": "no ok"}, "browser"), ("open_session", None, "HTTP")]
    for name, value, kind in cases:
        rc, out = run_summary(name, value)
        want = "B did not report %s check(s): %s" % (kind, name)
        if rc != 1 or want not in out or "Traceback" in out:
            print("FAIL: %s=%s on both sides: rc=%s, want 1 naming it:\n%s" % (name, json.dumps(value), rc, out))
            fail = 1
    rc, out = run_summary()
    if rc != 0:
        print("FAIL: every check reported: rc=%s, want 0:\n%s" % (rc, out))
        fail = 1
    if fail:
        return 1
    print("PASS: a check B reports as null or malformed fails the A/B; a complete run passes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
