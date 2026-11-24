"""tests/ab/ab.py summary: a B much slower than A fails the A/B (#151).

Every A/B check waits up to 30 s for its condition, so a change that answers
5 s late still passes each check.  The summary compares the two sides'
input_latency samples (median, floor plus ratio), which
browser.d/input_latency.js puts in its check.  Runs `ab.py summary` on
side folders written here (no server, no browser):
  - B 5 s per round trip, A 0.1 s: exit 1, names input_latency
  - A = B: exit 0
  - one slow B sample of seven: exit 0 (a median, never one reading)
  - both sides slow alike (a loaded host): exit 0 (the ratio)

Run: python3 -B tests/ab_latency_test.py
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ab")
AB = os.path.join(HERE, "ab.py")
# summary requires every page check ab.py knows (ORDER plus browser.d/), so
# take the list from ab.py, not a copy that goes stale when a check is added.
os.environ.pop("AB_A_DIR", None)
sys.path.insert(0, HERE)
import ab  # noqa: E402

# Every page check passes on both sides, so only the latency rule decides.
PAGE = [n for n in ab.all_checks() if n not in ab.HTTP_CHECKS and n != "input_latency"]


def write_side(root, label, latency):
    d = os.path.join(root, label)
    os.makedirs(d)
    checks = {n: {"ok": True, "value": "ok"} for n in PAGE}
    checks["input_latency"] = {"ok": True, "value": "7 wheel round trips handled", "samples": latency}
    with open(os.path.join(d, "browser.json"), "w", encoding="utf-8") as fh:
        json.dump({"browser": "test", "checks": checks}, fh)
    return d


def run_summary(alat, blat):
    with tempfile.TemporaryDirectory() as root:
        a = write_side(root, "A", alat)
        b = write_side(root, "B", blat)
        p = subprocess.run([sys.executable, "-B", AB, "summary", a, b],
                           capture_output=True, text=True, timeout=60)
        return p.returncode, p.stdout + p.stderr


def main():
    fast = [90, 110, 100, 95, 120, 105, 100]
    cases = [
        ("B 5 s late", fast, [5010, 5020, 5005, 5030, 5015, 5008, 5012], 1),
        ("A = B", fast, fast, 0),
        ("one slow B sample", fast, [90, 110, 100, 6000, 120, 105, 100], 0),
        ("both sides slow alike", [1500, 1700, 1600, 1550, 1650, 1580, 1620],
         [1600, 1800, 1700, 1650, 1750, 1680, 1720], 0),
    ]
    fail = 0
    for name, alat, blat, want in cases:
        rc, out = run_summary(alat, blat)
        named = "REGRESSION: passes on A, fails on B: input_latency" in out
        # The samples must not make A and B differ, and nothing else may fail.
        broken = "BROWSER FAILURE" in out or ("check(s) differ" in out and not want)
        if rc != want or named != bool(want) or broken:
            print("FAIL: %s: rc=%s, want %s:\n%s" % (name, rc, want, out))
            fail = 1
    if fail:
        return 1
    print("PASS: a B 5 s slower than A fails the A/B; equal, one-off and shared slowness pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
