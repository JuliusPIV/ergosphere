"""acquire_session_lock: a status probe that overlaps an acquisition must not look like another grok.

session_lock_busy takes the same session-folder flock for a moment. A probe
thread that is slow to drop it (GIL starved, descheduled) must not make
Resume or a new session's lock_when_created refuse. A real holder still refuses.

Run: python3 -B tests/session_lock_probe_test.py
Stdlib only, no network, no grok.
"""
import fcntl
import importlib.util
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SID = "00000000-0000-0000-0000-000000000022"


def load_module(folder):
    spec = importlib.util.spec_from_file_location("ergosphere", ROOT / "ergosphere.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.session_dir_for = lambda sid: folder  # the session folder is a temp dir here
    return mod


def check_stalled_probe_does_not_refuse(erg):
    """A probe holds the flock 0.2 s before closing it; the acquisition must still get the lock."""
    real_close = erg.close_fds
    probe_holds = threading.Event()

    def stalled_close(fds):
        probe_holds.set()
        time.sleep(0.2)
        real_close(fds)

    erg.close_fds = stalled_close
    try:
        probe = threading.Thread(target=erg.session_lock_busy, args=(SID,))
        probe.start()
        if not probe_holds.wait(5):
            return "the probe never took the lock"
        fds = erg.acquire_session_lock(SID)
        probe.join()
    finally:
        erg.close_fds = real_close
    if fds is None:
        return "a probe that overlapped the acquisition made it refuse (None): not another grok"
    erg.close_fds(fds)
    return None


def check_real_holder_still_refuses(erg, folder):
    """Another open holds the flock for good; the acquisition must return None."""
    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fds = erg.acquire_session_lock(SID)
        if fds is not None:
            erg.close_fds(fds)
            return "a lock held by another process was not refused"
    finally:
        os.close(fd)
    return None


def main():
    folder = tempfile.mkdtemp(prefix="ergo-probe-")
    erg = load_module(folder)
    failures = [
        check_stalled_probe_does_not_refuse(erg),
        check_real_holder_still_refuses(erg, folder),
    ]
    failures = [f for f in failures if f]
    for failure in failures:
        print("FAIL: " + failure)
    if failures:
        return 1
    print("PASS: a status probe never makes an acquisition refuse; a real holder still does")
    return 0


if __name__ == "__main__":
    sys.exit(main())
