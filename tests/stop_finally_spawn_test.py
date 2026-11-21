"""kill_all without STOP still owns a spawn that finishes while it waits (issue #45 review).

main() calls kill_all from its finally after serve_forever returns, also when
STOP was never set (an exception out of serve_forever).  A spawn in flight
then finishes, sees no STOP and adds its grok to LIVE after kill_all took its
snapshot.  kill_all must still shut that grok down before it returns.

Run: python3 -B tests/stop_finally_spawn_test.py
Stdlib only, no network, no real grok.
"""
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = tempfile.mkdtemp(prefix="ergo-stopfinally-")
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
    with open(PIDF, "w") as handle:
        handle.write(str(os.getpid()))
    os.open(FIFO, os.O_RDONLY)


def main():
    erg = load()
    real_popen = subprocess.Popen

    def blocking_popen(*args, **kwargs):
        kwargs["preexec_fn"] = block_before_exec
        return real_popen(*args, **kwargs)

    cleared = threading.Event()

    class Live(dict):
        def clear(self):
            dict.clear(self)
            cleared.set()

    erg.LIVE = Live()
    failures = []
    sid = str(uuid.uuid4())
    cmd = [sys.executable, "-c", "import time; time.sleep(120)", "--resume", sid]
    box = {}
    subprocess.Popen = blocking_popen
    spawn = threading.Thread(target=lambda: box.update(live=erg.open_live(sid, cmd, TMP, 80, 24)[0]), daemon=True)
    spawn.start()
    try:
        for _ in range(200):
            if os.path.exists(PIDF) and os.path.getsize(PIDF):
                break
            time.sleep(0.05)
        if not (os.path.exists(PIDF) and os.path.getsize(PIDF)):
            failures.append("the spawn never reached its pre-exec block")
            return report(failures)
        stop = threading.Thread(target=erg.kill_all, daemon=True)
        stop.start()
        if not cleared.wait(10):
            failures.append("kill_all never took its LIVE snapshot")
        os.close(os.open(FIFO, os.O_WRONLY))  # the mount recovers, STOP still unset
        spawn.join(10)
        stop.join(10)
        live = box.get("live")
        if stop.is_alive():
            failures.append("kill_all did not return after the spawn finished")
        elif live is None:
            failures.append("the spawn did not finish")
        elif live.proc.poll() is None:
            failures.append("a grok spawned during kill_all is still running after it: pid %d" % live.proc.pid)
            live.proc.kill()
            live.proc.wait()
    finally:
        subprocess.Popen = real_popen
        shutil.rmtree(TMP, ignore_errors=True)
    return report(failures)


def report(failures):
    for failure in failures:
        print("FAIL: " + failure)
    if failures:
        return 1
    print("PASS: kill_all without STOP shuts down a grok whose spawn ends while it waits")
    return 0


if __name__ == "__main__":
    sys.exit(main())
