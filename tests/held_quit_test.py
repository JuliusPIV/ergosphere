"""A page whose session another Ergosphere server runs offers no Quit (#134).

That page owns no grok and no terminal socket, so Quit there set "quitting"
and nothing ever ended it.  Runs tests/ab/held_quit.py on this checkout: two
throwaway servers on one HOME, the stand-in grok in one, Chrome on the other,
through every way a page gets into or out of the held state (opened held,
Resume into held, Quit while that Resume is pending, take-over ok and refused,
two tabs, the page's server stopping).

Run: NODE_PATH=<node_modules with playwright> python3 -B tests/held_quit_test.py
Uses ERGOSPHERE_TEST_PORT and the port after it when set (run_all), else two free ports.
Skipped without node, playwright or Chrome (/usr/bin/google-chrome or ERGO_CHROME).
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def ports():
    if os.environ.get("ERGOSPHERE_TEST_PORT"):
        first = int(os.environ["ERGOSPHERE_TEST_PORT"])
        return first, first + 1
    out = subprocess.run([sys.executable, "-I", str(ROOT / "tests" / "free_port.py"), "2"],
                         capture_output=True, text=True, check=True)
    first = int(out.stdout.strip())
    return first, first + 1


def main():
    tmp = tempfile.mkdtemp(prefix="ergo-held-quit-")
    try:
        port, hold_port = ports()
        out = subprocess.run([sys.executable, "-B", str(ROOT / "tests" / "ab" / "held_quit.py"), "run",
                              str(ROOT), str(port), str(hold_port), tmp],
                             capture_output=True, text=True, timeout=500, cwd=tmp)
        try:
            res = json.loads(out.stdout)
        except ValueError:
            print("FAIL held_quit.py gave no result: %s" % out.stderr.strip()[-500:])
            return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if "unavailable" in res:
        print("SKIP browser: %s" % res["unavailable"])
        return 0
    fails = []
    for name, c in res["checks"].items():
        print("%s %s: %s" % ("ok  " if c["ok"] else "FAIL", name, c["value"]))
        if not c["ok"]:
            fails.append(name)
    if res.get("error"):
        print("browser error: %s" % res["error"])
    print("PASS" if not fails else "FAIL: %s" % ", ".join(fails))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
