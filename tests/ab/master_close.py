"""A/B check for issue #218: a Live's PTY master is closed once.

  python3 -B master_close.py run <code-dir>        -> JSON checks on stdout
  python3 -B master_close.py summary <A-json> <B-json>  -> table on stdout;
                                                     exit 1 when a check fails on B

run runs tests/master_close_test.py (from this tree) against <code-dir>'s
ergosphere.py: each check forces one close order (Quit, grok exit, two Quits,
server stop, reader held in poll), puts an unrelated file at the master's old
fd number and reports whether that file survived; resize_after_close reports
whether a resize after the close is a quiet no-op.  Stdlib only.
"""
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TEST = os.path.join(os.path.dirname(HERE), "master_close_test.py")
ORDER = ["quit_then_reader", "grok_exit_then_shutdown", "quit_twice",
         "server_stop_then_reader", "reader_blocked_in_poll", "resize_after_close"]


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
            kept = "no error" if name == "resize_after_close" else "unrelated fd kept"
            checks[name] = {"ok": word == "ok", "value": kept if word == "ok" else why}
    return {"checks": checks, "seconds": round(time.time() - start, 2)}


def summary(apath, bpath):
    load = lambda p: (json.load(open(p)).get("checks") or {}) if os.path.exists(p) else {}
    a, b = load(apath), load(bpath)
    width = 44
    lines = ["", "Issue #218: PTY master closed twice (unrelated file planted at the old fd number)",
             "%-24s %-*s %-*s" % ("check", width, "A", width, "B")]
    bad = []
    for name in ORDER:
        ca = a.get(name) or {"ok": False, "value": "not run"}
        cb = b.get(name) or {"ok": False, "value": "not run"}
        cell = lambda c: ("%s %s" % ("PASS" if c["ok"] else "FAIL", c["value"]))[:width]
        if not cb["ok"]:
            bad.append(name)
        lines.append("%-24s %-*s %-*s" % (name, width, cell(ca), width, cell(cb)))
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
