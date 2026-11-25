"""tests/ab/ab.py pidreuse: B's pid reuse run must exit 0 and report every check.

Runs `ab.py pidreuse` on outputs written here (no namespace, no process):
a run that crashed after one passing row, a run with one check dropped, a run
that reported every row but exited non-zero, and a skipped run must each exit
1; a complete passing run must exit 0.

Run: python3 -B tests/ab_pid_reuse_summary_test.py
"""
import importlib.util
import os
import subprocess
import sys
import tempfile

AB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ab", "ab.py")
spec = importlib.util.spec_from_file_location("ab", AB)
ab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ab)
ALL = ab.PID_REUSE_CHECKS


def output(names, status, tail=""):
    return "".join("ok: %s\n" % n for n in names) + tail + "exit: %d\n" % status


def run(b_text):
    with tempfile.TemporaryDirectory() as root:
        a, b = os.path.join(root, "A.txt"), os.path.join(root, "B.txt")
        with open(a, "w") as fh:
            fh.write(output(ALL, 0))
        with open(b, "w") as fh:
            fh.write(b_text)
        p = subprocess.run([sys.executable, "-B", AB, "pidreuse", a, b],
                           capture_output=True, text=True, timeout=60)
        return p.returncode, p.stdout + p.stderr


CASES = [
    ("crashed after one passing row",
     output(ALL[:1], 1, "Traceback (most recent call last):\nRuntimeError: boom\n"), 1, "did not report"),
    ("one check dropped", output(ALL[:-1], 0, "PASS\n"), 1, ALL[-1]),
    ("every row but exit 1", output(ALL, 1), 1, "exited 1"),
    ("skipped", "SKIP: no unprivileged user and pid namespace here\nexit: 0\n", 1, "did not report"),
    ("complete", output(ALL, 0, "PASS\n"), 0, ""),
]


def main():
    fail = 0
    spec = importlib.util.spec_from_file_location(
        "pid_reuse_test", os.path.join(os.path.dirname(AB), "..", "pid_reuse_test.py"))
    reuse = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reuse)
    names = [n for n, _check in reuse.CHECKS]
    if names != ALL:
        print("FAIL: ab.py PID_REUSE_CHECKS %r != pid_reuse_test.py CHECKS %r" % (ALL, names))
        fail = 1
    else:
        print("ok: ab.py names every pid_reuse_test.py check")
    for name, text, want, needle in CASES:
        rc, out = run(text)
        if rc != want or needle not in out:
            print("FAIL: %s: rc=%s, want %s with %r:\n%s" % (name, rc, want, needle, out))
            fail = 1
        else:
            print("ok: %s" % name)
    print("FAIL" if fail else "PASS: a crashed, partial, failing or skipped pid reuse run fails the A/B")
    return fail


if __name__ == "__main__":
    sys.exit(main())
