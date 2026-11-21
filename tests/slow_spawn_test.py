"""A slow grok start blocks only that session (issue #45).

The start of one session is made to hang the way a cwd on a stalled network
mount hangs it: its spawn waits on a fifo nobody writes yet. Meanwhile another
session must still launch, a running session must still reattach and state
reads must still answer. A second start of the hanging session must be
refused, not wait or start a second grok, and once the hang ends that
session has exactly one grok.

Run: python3 -B tests/slow_spawn_test.py
Stdlib only, no network, no real grok: "grok" is python under that name.
"""
import contextlib
import importlib.util
import io
import os
import shutil
import sys
import tempfile
import threading
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = tempfile.mkdtemp(prefix="ergo-slowspawn-")
GROK = os.path.join(TMP, "grok")
FIFO = os.path.join(TMP, "mount")
os.symlink(sys.executable, GROK)
os.mkfifo(FIFO)
os.environ["HOME"] = TMP
WAIT = 3.0


def load():
    spec = importlib.util.spec_from_file_location("ergosphere", ROOT / "ergosphere.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def cmd(sid):
    return [GROK, "-c", "import time; time.sleep(120)", "--resume", sid]


def call(fn, *args, wait=WAIT):
    """Run fn in a thread; return the thread and its result or exception box after wait seconds."""
    box = {}

    def run():
        try:
            box["value"] = fn(*args)
        except BaseException as exc:
            box["value"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(wait)
    return worker, box


def main():
    erg = load()
    slow, other = str(uuid.uuid4()), str(uuid.uuid4())
    real_spawn = erg.spawn_grok
    spawned = []
    entered = threading.Event()

    def spawn(argv, cwd, cols, rows):
        sid = argv[-1]
        spawned.append(sid)
        if sid == slow:
            entered.set()
            with open(FIFO, "rb"):
                pass
        return real_spawn(argv, cwd, cols, rows)

    erg.spawn_grok = spawn
    failures = []
    writer = None
    with contextlib.redirect_stdout(io.StringIO()):
        try:
            running, _ = erg.open_live(other, cmd(other), TMP, 80, 24)
            first, first_box = call(erg.open_live, slow, cmd(slow), TMP, 80, 24, wait=0)
            if not entered.wait(WAIT):
                failures.append("the slow start never reached its spawn")
            launch, launch_box = call(erg.open_live, str(uuid.uuid4()), cmd("new"), TMP, 80, 24)
            if launch.is_alive():
                failures.append("a new launch waited for another session's slow start")
            reattach, re_box = call(erg.open_live, other, cmd(other), TMP, 80, 24)
            if reattach.is_alive():
                failures.append("reattach to a running session waited for another session's slow start")
            elif re_box.get("value") != (running, True):
                failures.append("reattach did not return the running grok: %r" % (re_box.get("value"),))
            info, info_box = call(erg.session_state, slow, False)
            if info.is_alive():
                failures.append("the state read waited for the slow start")
            elif info_box["value"]["state"] != "starting":
                failures.append("the slow session reads %r, not starting" % info_box["value"]["state"])
            second, second_box = call(erg.open_live, slow, cmd(slow), TMP, 80, 24)
            if second.is_alive():
                failures.append("a second start of the slow session waited instead of being refused")
            else:
                exc = second_box["value"]
                if not (isinstance(exc, erg.SessionBusy) and exc.state == "starting"):
                    failures.append("a second start of the slow session was not refused as starting: %r" % (exc,))
            writer = os.open(FIFO, os.O_WRONLY)
            for worker in (first, launch, reattach, info, second):
                worker.join(WAIT * 2)
            if first.is_alive() or not isinstance(first_box.get("value"), tuple):
                failures.append("the slow start did not finish: %r" % (first_box.get("value"),))
            if spawned.count(slow) != 1:
                failures.append("the slow session was spawned %d times" % spawned.count(slow))
            live = erg.LIVE.get(slow)
            if live is None or not live.alive():
                failures.append("the slow session has no running grok after the hang ended")
            if slow in getattr(erg, "STARTING", ()):
                failures.append("the slow session stays marked starting")
        finally:
            if writer is None and entered.is_set():
                try:
                    writer = os.open(FIFO, os.O_WRONLY | os.O_NONBLOCK)
                except OSError:
                    pass
            erg.kill_all()
            if writer is not None:
                os.close(writer)
            shutil.rmtree(TMP, ignore_errors=True)
    for failure in failures:
        print("FAIL: " + failure)
    if failures:
        return 1
    print("PASS: a slow start blocks only its own session and starts one grok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
