"""A/B check for issue #132: SIGTERM/SIGINT stop the server wherever they land.

  python3 -B sigterm_lock.py run <code-dir> <port>   -> JSON checks on stdout
  python3 -B sigterm_lock.py summary <A-json> <B-json> -> table on stdout;
                                                       exit 1 when a check passes on A and fails on B

Runs the cases of tests/sigterm_lock_test.py (this tree's copy) against
<code-dir>/ergosphere.py.  Stdlib only.
"""
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def load_test():
    spec = importlib.util.spec_from_file_location(
        "sigterm_lock_test", os.path.join(os.path.dirname(HERE), "sigterm_lock_test.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run(code, port):
    test = load_test()
    return {"checks": {case: {"ok": ok, "value": detail[:60]}
                       for case, (ok, detail) in test.run_all(os.path.abspath(code), int(port)).items()}}


def summary(apath, bpath):
    order = load_test().CASES
    load = lambda p: (json.load(open(p)).get("checks") or {}) if os.path.exists(p) and os.path.getsize(p) else {}
    a, b = load(apath), load(bpath)
    width = 46
    lines = ["", "Issue #132: SIGTERM/SIGINT landing at one exact point; the server must exit 0",
             "%-10s %-*s %-*s %s" % ("check", width, "A", width, "B", "same")]
    regress = []
    for name in order:
        ca = a.get(name) or {"ok": False, "value": "not run"}
        cb = b.get(name) or {"ok": False, "value": "not run"}
        cell = lambda c: ("%s %s" % ("PASS" if c["ok"] else "FAIL", c["value"]))[:width]
        if not cb["ok"] and (ca["ok"] or name not in a):
            regress.append(name)
        lines.append("%-10s %-*s %-*s %s" % (name, width, cell(ca), width, cell(cb), "yes" if ca == cb else "NO"))
    failing = [n for n in order if not (b.get(n) or {}).get("ok")]
    if regress:
        lines.append("REGRESSION: passes on A, fails on B: %s" % ", ".join(regress))
    if failing:
        lines.append("FAIL on B: %s" % ", ".join(failing))
    print("\n".join(lines))
    return 1 if failing else 0


def main(argv):
    if len(argv) == 4 and argv[1] == "run":
        print(json.dumps(run(argv[2], argv[3])))
        return 0
    if len(argv) == 4 and argv[1] == "summary":
        return summary(argv[2], argv[3])
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
