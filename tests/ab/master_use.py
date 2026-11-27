"""A/B check for issue #227: no write, resize or read reaches a reused master fd.

  python3 -B master_use.py run <code-dir>          -> JSON checks on stdout
  python3 -B master_use.py summary <A-json> <B-json>  -> table on stdout;
                                                     exit 1 when a check fails on B

run runs tests/master_use_test.py (from this tree) against <code-dir>'s
ergosphere.py: each check closes the master between a caller taking its fd
and the syscall (write, resize, repaint, read, two tabs, server stop, a failed
write, a write blocked on a full PTY), puts an unrelated file or PTY at the
number if it was freed, and reports whether that file was touched and the
master closed once its users finished.  Stdlib only.
"""
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TEST = os.path.join(os.path.dirname(HERE), "master_use_test.py")
ORDER = ["quit_during_write", "quit_during_resize", "quit_during_repaint",
         "quit_during_read", "second_tab_write_and_resize", "server_stop_during_write",
         "write_fails_mid_way", "quit_during_blocked_write"]


def run(code):
    start = time.time()
    try:
        out = subprocess.run([sys.executable, "-B", TEST, code], capture_output=True,
                             text=True, timeout=120).stdout
    except subprocess.TimeoutExpired:
        out = ""
    checks = {}
    for line in out.splitlines():
        word, _, rest = line.partition(" ")
        name, _, why = rest.partition(": ")
        if word in ("ok", "FAIL") and name in ORDER:
            checks[name] = {"ok": word == "ok", "value": "no reused fd touched" if word == "ok" else why}
    return {"checks": checks, "seconds": round(time.time() - start, 2)}


def summary(apath, bpath):
    load = lambda p: (json.load(open(p)).get("checks") or {}) if os.path.exists(p) else {}
    a, b = load(apath), load(bpath)
    width = 48
    lines = ["", "Issue #227: syscall on the master after its close (unrelated file planted at the old fd number)",
             "%-28s %-*s %-*s" % ("check", width, "A", width, "B")]
    bad = []
    for name in ORDER:
        ca = a.get(name) or {"ok": False, "value": "not run"}
        cb = b.get(name) or {"ok": False, "value": "not run"}
        cell = lambda c: ("%s %s" % ("PASS" if c["ok"] else "FAIL", c["value"]))[:width]
        if not cb["ok"]:
            bad.append(name)
        lines.append("%-28s %-*s %-*s" % (name, width, cell(ca), width, cell(cb)))
    if bad:
        lines.append("FAIL on B: %s" % ", ".join(bad))
    print("\n".join(lines))
    return 1 if bad else 0


def main(argv):
    if len(argv) == 3 and argv[1] == "run":
        print(json.dumps(run(argv[2])))
        return 0
    if len(argv) == 4 and argv[1] == "summary":
        return summary(argv[2], argv[3])
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
