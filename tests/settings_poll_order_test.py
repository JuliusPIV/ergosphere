"""Settings applies slow /session-info replies that land in order (#155).

After #125 the page dropped every poll reply that was not from the latest
poll, so when replies took longer than the 2 s poll interval each one was
superseded before it landed and the count never updated.  A reply must be
dropped only when it is older than the last applied reply or the last
Restart click.

Each scenario runs the real Settings page script in node against the stub
DOM and fetch of settings_restart_poll_test.py, with every /session-info
reply held until a step releases it (oldest or newest first).

Run: python3 -B tests/settings_poll_order_test.py
Stdlib plus node, no network, no grok, no server.  Fails without node.
"""
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("restart_poll", HERE / "settings_restart_poll_test.py")
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)

BUSY = "1 grok session running; restart needs zero"
ZERO = {"sessions": [], "running": 0}
ONE = {"sessions": [{"id": "e", "alive": True}], "running": 1}
HOLD = {"hold": True, "body": None}

# (name, steps, expected (count, disabled, msg or None) after each step or None to skip)
SCENARIOS = [
    ("slow replies in order (each lands after the next poll went out)",
     [HOLD, HOLD, {"release": ZERO}, HOLD, {"release": ZERO}, HOLD, {"release": ZERO}],
     [None, None, (0, False, None), None, (0, False, None), None, (0, False, None)]),
    ("out of order: newer reply first, older one dropped",
     [HOLD, HOLD, {"releaseNewest": ZERO}, {"release": ONE}],
     [None, None, (0, False, None), (0, False, None)]),
    ("failed newer reply, then older good reply is applied",
     [HOLD, HOLD, {"releaseNewest": None}, {"release": ZERO}],
     [None, None, (1, True, None), (0, False, None)]),
    ("poll out, Restart refused with a count, old reply dropped, newer poll applied",
     [HOLD, {"hold": True, "click": True, "refuse": {"ok": False, "sessions": 1, "error": BUSY}, "body": None},
      {"release": ZERO}, {"release": ZERO}],
     [None, (1, True, BUSY), (1, True, BUSY), (0, False, "")]),
    ("poll out, Restart in progress, old reply dropped",
     [HOLD, {"hold": True, "click": True, "body": None}, {"release": ZERO}],
     [None, (1, True, "Restarting..."), (1, True, "Restarting...")]),
]


def run(erg, steps, tmp):
    state = {"bind": "", "port": "", "runningBind": "127.0.0.1", "runningPort": 1, "binds": ["127.0.0.1"],
             "agents": {}, "names": [], "overrides": [], "sessions": 1}
    script = Path(tmp, "settings.js")
    script.write_text(base.page_script(erg.settings_page(state)))
    steps_file = Path(tmp, "steps.json")
    steps_file.write_text(json.dumps(steps))
    driver = Path(tmp, "driver.js")
    driver.write_text(base.DRIVER)
    out = subprocess.run(["node", str(driver), str(script), str(steps_file)],
                         capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError("node exited %d: %s" % (out.returncode, out.stderr.strip()[-400:]))
    return json.loads(out.stdout)


def main():
    if not shutil.which("node"):
        print("FAIL: node not found")
        return 1
    erg = base.load()
    failed = 0
    with tempfile.TemporaryDirectory() as tmp:
        for name, steps, want in SCENARIOS:
            got = run(erg, steps, tmp)
            bad = []
            for i, (w, seen) in enumerate(zip(want, got)):
                if w is None:
                    continue
                count, disabled, msg = w
                ok = (seen["text"].startswith("Running in this server: %d." % count)
                      and seen["disabled"] == disabled and (msg is None or seen["msg"] == msg))
                if not ok:
                    bad.append("step %d: want %d/%s/%r, page shows %r, button %s, message %r" % (
                        i + 1, count, "disabled" if disabled else "enabled", msg, seen["text"],
                        "disabled" if seen["disabled"] else "enabled", seen["msg"]))
            print("%s %s%s" % ("FAIL" if bad else "PASS", name, "".join("\n  " + b for b in bad)))
            failed += bool(bad)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
