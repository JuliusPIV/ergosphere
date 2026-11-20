"""A Resume that races another Resume's start reattaches with 200 (issue #45 review).

Tab 1 resumes a session and is still in open_live's holder scan, before the
id is in STARTING.  Tab 2 resumes the same session: it finds no spawn to wait
for and reads the session as stopped, then meets the id in STARTING in
open_live.  It must wait for that start and answer 200 (main waited on
LIVE_LOCK), not 409 "starting", which the page shows as Ended.  Then many
Resumes at once during a hung start: every one answers 200 and one grok runs.

Run: python3 -B tests/resume_race_test.py
Stdlib only, no network, no real grok.  The order is forced with events.
"""
import contextlib
import importlib.util
import io
import os
import shutil
import sys
import tempfile
import threading
import types
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = tempfile.mkdtemp(prefix="ergo-resume-race-")
os.environ["HOME"] = TMP
WAIT = 10


def load():
    spec = importlib.util.spec_from_file_location("ergosphere", ROOT / "ergosphere.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def resume(erg, sid, box):
    replies = []
    handler = types.SimpleNamespace(
        _json_object=lambda: {"id": sid},
        _json=lambda code, body: replies.append(code),
    )
    handler._resume_once = lambda s: erg.Handler._resume_once(handler, s)
    erg.Handler._resume(handler)
    box[threading.current_thread().name] = replies[0] if replies else None


def main():
    erg = load()
    sid = str(uuid.uuid4())
    erg.find_session = lambda s: {"id": s, "cwd": TMP}
    erg.grok_argv = lambda agent, approval, cwd, resume, *a, **k: [sys.executable, "-c", "import time; time.sleep(60)", resume]
    real_spawn, real_holder = erg.spawn_grok, erg.session_holder
    spawns = []
    spawning, release = threading.Event(), threading.Event()

    def spawn(cmd, cwd, cols, rows):
        spawns.append(cmd[-1])
        spawning.set()
        release.wait(WAIT)
        return real_spawn(cmd, cwd, cols, rows)

    # open_live's holder scan (one argument) is where tab 1 and tab 2 are held.
    at_scan = {"tab1": threading.Event(), "tab2": threading.Event()}
    go = {"tab1": threading.Event(), "tab2": threading.Event()}

    def holder(s, scan=None):
        name = threading.current_thread().name
        if scan is None and name in at_scan:
            at_scan[name].set()
            go[name].wait(WAIT)
        return real_holder(s, scan)

    # Tab 2 has met tab 1's start once it waits for a spawn a second time
    # (the fix) or has answered (main: 409, or 200 had the start finished first).
    real_wait = erg.wait_for_spawn
    waits = []
    rewait = threading.Event()

    def wait_for_spawn(s):
        if threading.current_thread().name == "tab2":
            waits.append(s)
            if len(waits) == 2:
                rewait.set()
        return real_wait(s)

    erg.spawn_grok, erg.session_holder, erg.wait_for_spawn = spawn, holder, wait_for_spawn
    failures = []
    box = {}
    with contextlib.redirect_stdout(io.StringIO()):
        try:
            tab1 = threading.Thread(target=resume, args=(erg, sid, box), name="tab1", daemon=True)
            tab2 = threading.Thread(target=resume, args=(erg, sid, box), name="tab2", daemon=True)
            tab1.start()
            at_scan["tab1"].wait(WAIT)
            tab2.start()
            if not at_scan["tab2"].wait(WAIT):
                failures.append("tab 2 never reached open_live")
            go["tab1"].set()
            if not spawning.wait(WAIT):
                failures.append("tab 1 never started its spawn")
            go["tab2"].set()
            for _ in range(WAIT * 20):
                if not tab2.is_alive() or rewait.wait(0.05):
                    break
            many = [threading.Thread(target=resume, args=(erg, sid, box), name="more%d" % i, daemon=True)
                    for i in range(8)]
            for worker in many:
                worker.start()
            release.set()
            for worker in [tab1, tab2] + many:
                worker.join(WAIT)
            if box.get("tab1") != 200:
                failures.append("tab 1 Resume answered %r" % box.get("tab1"))
            if box.get("tab2") != 200:
                failures.append("tab 2 Resume that raced tab 1's start answered %r, not 200" % box.get("tab2"))
            others = sorted(set(box.get("more%d" % i) for i in range(8)), key=str)
            if others != [200]:
                failures.append("concurrent Resumes answered %r" % others)
            if len(spawns) != 1:
                failures.append("the session was spawned %d times" % len(spawns))
        finally:
            release.set()
            for event in go.values():
                event.set()
            erg.kill_all()
            shutil.rmtree(TMP, ignore_errors=True)
    for failure in failures:
        print("FAIL: " + failure)
    if failures:
        return 1
    print("PASS: a Resume racing another Resume's start reattaches; one grok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
