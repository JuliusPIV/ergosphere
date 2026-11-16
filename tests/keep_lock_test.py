"""keep_lock_for: the session lock lives as long as grok, on every Python 3.

os.pidfd_open is Python 3.9+. Older Pythons must keep the lock by polling the
pid, or every Resume is refused and every new session is stopped at launch.
Each check runs twice: once with os.pidfd_open and once with it removed.

Run: python3 -B tests/keep_lock_test.py
Stdlib only, no network, no grok: a plain sleep stands in for it.
"""
import fcntl
import importlib.util
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_module():
    spec = importlib.util.spec_from_file_location("ergosphere", ROOT / "ergosphere.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def lock_held(folder):
    """True when another open of folder cannot take its flock."""
    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(fd)
    return False


def wait_until(pred, secs):
    end = time.time() + secs
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


def check_mode(erg, label, folder):
    """Hold the folder lock for a running child; it must outlive the keep call and end with the child."""
    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # what acquire_session_lock does
    proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        erg.keep_lock_for(proc, [fd])  # takes fd; closes it
    except erg.SessionLockUnavailable as exc:
        proc.kill()
        proc.wait()
        return "%s: keep_lock_for refused: %s" % (label, exc)
    try:
        # Sustained, not momentary: a keeper that dies at once still holds the lock for a few ms.
        time.sleep(1)
        if not lock_held(folder):
            return "%s: the lock was not held while grok runs" % label
        proc.kill()
        proc.wait()
        if not wait_until(lambda: not lock_held(folder), 3):
            return "%s: the lock was not released after grok exited" % label
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    return None


def check_high_fd(erg, folder):
    """The keeper must work when its pidfd number is at or above 1024 (select() limit)."""
    held = []
    try:
        while True:
            fd = os.open(os.devnull, os.O_RDONLY)
            if fd >= 1030:
                os.close(fd)
                break
            held.append(fd)
        return check_mode(erg, "pidfd_open at fd >= 1024", folder)
    finally:
        for fd in held:
            os.close(fd)


def main():
    erg = load_module()
    folder = tempfile.mkdtemp(prefix="ergo-keep-")
    real = getattr(os, "pidfd_open", None)
    failures = []
    try:
        failures.append(check_mode(erg, "pidfd_open", folder))
        if real is not None:
            failures.append(check_high_fd(erg, folder))
        if real is not None:
            del os.pidfd_open  # emulate Python < 3.9
        failures.append(check_mode(erg, "no pidfd_open", folder))
    finally:
        if real is not None:
            os.pidfd_open = real
    failures = [f for f in failures if f]
    for failure in failures:
        print("FAIL: " + failure)
    if failures:
        return 1
    print("PASS: the session lock lives as long as grok, with and without os.pidfd_open")
    return 0


if __name__ == "__main__":
    sys.exit(main())
