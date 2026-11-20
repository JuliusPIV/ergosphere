"""Stop waits for a spawn in flight, so its child is never left unowned (issue #45 review).

A spawn runs with LIVE_LOCK released, so a child stuck before exec (its cwd on a
hung mount) is in no LIVE entry. Stop must not return and let the server exit
while that child is still pending: once the mount recovers the child would exec
grok with no owner. The spawn's own open_live then kills it, because STOP is set.
A launch after STOP must be refused, not start.

Run: python3 -B tests/stop_pending_spawn_test.py
Stdlib only, no network, no real grok.
"""
import importlib.util
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = tempfile.mkdtemp(prefix="ergo-stoppending-")
FIFO = os.path.join(TMP, "mount")
PIDF = os.path.join(TMP, "child.pid")
os.mkfifo(FIFO)
os.environ["HOME"] = TMP


def load():
    spec = importlib.util.spec_from_file_location("ergosphere", ROOT / "ergosphere.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def block_before_exec():
    # preexec_fn runs in the forked child before exec, where a hung chdir sits.
    with open(PIDF, "w") as handle:
        handle.write(str(os.getpid()))
    os.open(FIFO, os.O_RDONLY)


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def main():
    erg = load()
    real_popen = subprocess.Popen

    def blocking_popen(*args, **kwargs):
        kwargs["preexec_fn"] = block_before_exec
        return real_popen(*args, **kwargs)

    failures = []
    # A running session whose grok ignores SIGHUP, started before spawns block.
    running_sid = str(uuid.uuid4())
    hardy = [sys.executable, "-c", "import signal,time; signal.signal(signal.SIGHUP, signal.SIG_IGN); time.sleep(120)",
             "--resume", running_sid]
    running, _ = erg.open_live(running_sid, hardy, TMP, 80, 24)
    subprocess.Popen = blocking_popen
    sid = str(uuid.uuid4())
    cmd = [sys.executable, "-c", "import time; time.sleep(120)", "--resume", sid]
    threading.Thread(target=lambda: erg.open_live(sid, cmd, TMP, 80, 24), daemon=True).start()
    try:
        for _ in range(200):
            if os.path.exists(PIDF) and os.path.getsize(PIDF):
                break
            time.sleep(0.05)
        if not (os.path.exists(PIDF) and os.path.getsize(PIDF)):
            failures.append("the spawn never reached its pre-exec block")
            return report(failures)
        child = int(open(PIDF).read())

        # What _stop() does first, then kill_all; the server exits once kill_all returns.
        erg.STOP.set()
        stop = threading.Thread(target=erg.kill_all, daemon=True)
        stop.start()
        stop.join(1.0)
        if not stop.is_alive():
            failures.append("stop returned while a spawn was still pending: its child is unowned")
        # Its reader leaves LIVE on STOP; stop must still own that grok.
        for _ in range(100):
            with erg.LIVE_LOCK:
                if running_sid not in erg.LIVE:
                    break
            time.sleep(0.05)

        os.close(os.open(FIFO, os.O_WRONLY))  # the mount recovers
        stop.join(10)
        if alive(child):
            failures.append("the pending child is still running after stop: pid %d" % child)
            os.kill(child, 9)
        if running.proc.poll() is None:
            failures.append("a session running before stop is still running after it: pid %d" % running.proc.pid)
            running.proc.kill()

        # A launch after stop is refused before any spawn. Run it in a thread: if it
        # spawns instead, its pre-exec block would hang this test.
        os.remove(PIDF)
        box = {}

        def late_launch():
            try:
                erg.open_live(str(uuid.uuid4()), cmd, TMP, 80, 24)
                box["value"] = "started"
            except RuntimeError as exc:
                box["value"] = str(exc)

        launch = threading.Thread(target=late_launch, daemon=True)
        launch.start()
        launch.join(5)
        if os.path.exists(PIDF):
            failures.append("a launch after stop spawned a grok")
            os.close(os.open(FIFO, os.O_WRONLY))  # unblock that child so it can be cleaned up
        elif "stopping" not in box.get("value", ""):
            failures.append("a launch after stop was not refused as stopping: %r" % box.get("value"))
    finally:
        subprocess.Popen = real_popen
    return report(failures)


def report(failures):
    for failure in failures:
        print("FAIL: " + failure)
    if failures:
        return 1
    print("PASS: stop waits for a pending spawn, kills its child, refuses new launches")
    return 0


if __name__ == "__main__":
    sys.exit(main())
