"""No log write, HTTP reply or grok kill wait happens while LIVE_LOCK is held.

A stalled stdout (log on a slow disk), a slow client or a kill that waits
for grok to exit must not hold the lock that every launch, resume and
reattach needs.

Run: python3 -B tests/live_lock_io_test.py
Stdlib only, no network, no grok, no server.
"""
import importlib.util
import os
import sys
import threading
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_module():
    spec = importlib.util.spec_from_file_location("ergosphere", ROOT / "ergosphere.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeProc:
    pid = 4242

    def poll(self):
        return None


class FakeLive:
    def __init__(self, sid, proc, master, cwd):
        self.sid = sid
        self.proc = proc

    def alive(self):
        return True

    def start(self):
        pass

    def lock_when_created(self):
        pass


def lock_free_during(erg, label, block_event, release_event, target):
    """Run target in a thread; once it blocks on I/O, check LIVE_LOCK is free."""
    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    if not block_event.wait(5):
        release_event.set()
        worker.join(5)
        return "%s: the blocking write was never reached" % label
    got = erg.LIVE_LOCK.acquire(timeout=1)
    if got:
        erg.LIVE_LOCK.release()
    release_event.set()
    worker.join(5)
    if not got:
        return "%s: LIVE_LOCK was held while the write was blocked" % label
    return None


def check_open_live(erg):
    entered = threading.Event()
    release = threading.Event()

    class SlowStdout:
        def write(self, text):
            entered.set()
            release.wait(10)
            return len(text)

        def flush(self):
            pass

    erg.spawn_grok = lambda cmd, cwd, cols, rows: (FakeProc(), -1)
    erg.Live = FakeLive
    real = sys.stdout
    sys.stdout = SlowStdout()
    try:
        return lock_free_during(
            erg, "open_live log line", entered, release,
            lambda: erg.open_live("sid-open", ["grok"], "/", 80, 24))
    finally:
        sys.stdout = real
        erg.LIVE.clear()


def check_resume_reply(erg):
    entered = threading.Event()
    release = threading.Event()
    erg.LIVE["sid-resume"] = FakeLive("sid-resume", FakeProc(), -1, "/")

    def slow_json(code, obj):
        entered.set()
        release.wait(10)

    handler = types.SimpleNamespace(
        _json_object=lambda: {"id": "sid-resume"},
        _json=slow_json,
    )
    try:
        return lock_free_during(
            erg, "resume reply", entered, release,
            lambda: erg.Handler._resume(handler))
    finally:
        erg.LIVE.clear()


def check_failed_lock_kill(erg):
    """A grok stopped because its lock could not be kept is killed outside LIVE_LOCK."""
    entered = threading.Event()
    release = threading.Event()
    devnull = os.open(os.devnull, os.O_RDONLY)

    def refuse(proc, fds):
        raise erg.SessionLockUnavailable("test: keeper cannot start")

    def slow_kill(proc):
        entered.set()
        release.wait(10)

    saved = (erg.acquire_session_lock, erg.keep_lock_for, erg.kill_proc, erg.spawn_grok)
    erg.acquire_session_lock = lambda sid: [devnull]
    erg.keep_lock_for = refuse
    erg.kill_proc = slow_kill
    erg.spawn_grok = lambda cmd, cwd, cols, rows: (FakeProc(), os.open(os.devnull, os.O_RDONLY))
    try:
        return lock_free_during(
            erg, "stop after lock failure", entered, release,
            lambda: _open_live_quietly(erg, "sid-orphan"))
    finally:
        erg.acquire_session_lock, erg.keep_lock_for, erg.kill_proc, erg.spawn_grok = saved
        erg.LIVE.clear()


def _open_live_quietly(erg, sid):
    try:
        erg.open_live(sid, ["grok"], "/", 80, 24)
    except RuntimeError:
        pass  # the refusal is the expected result here


def check_proc_scan_outside_lock(erg):
    """The /proc walk (every process's stat and cmdline) is slow work: LIVE_LOCK must be free for it."""
    held_during_scan = []
    real_scan = erg.ProcScan

    class TracingScan(real_scan):
        def __init__(self):
            got = erg.LIVE_LOCK.acquire(timeout=0)
            if got:
                erg.LIVE_LOCK.release()
            else:
                held_during_scan.append(True)
            real_scan.__init__(self)

    erg.ProcScan = TracingScan
    erg.spawn_grok = lambda cmd, cwd, cols, rows: (FakeProc(), -1)
    erg.Live = FakeLive
    try:
        erg.open_live("sid-scan", ["grok"], "/", 80, 24)
    except RuntimeError:
        pass
    finally:
        erg.ProcScan = real_scan
        erg.LIVE.clear()
    if held_during_scan:
        return "proc scan: LIVE_LOCK was held while /proc was walked"
    return None


def check_holder_fd_walk_outside_lock(erg):
    """Reading a grok's open files (/proc/<pid>/fd) is /proc work: LIVE_LOCK must be free for it."""
    held = []
    real_scan = erg.ProcScan

    class TracingScan(real_scan):
        def __init__(self):
            real_scan.__init__(self)
            self.groks = [1]  # pid 1 stands in for a grok; its fds are read for the holder check
            self.cmd[1] = b"grok\0"

        def fd_targets(self, pid):
            # Only a real /proc read counts; a cached answer does no I/O.
            if pid not in self._fds and erg.LIVE_LOCK.locked():
                held.append(pid)
            return real_scan.fd_targets(self, pid)

    saved = (erg.acquire_session_lock, erg.keep_lock_for, erg.spawn_grok)
    dev = os.open(os.devnull, os.O_RDONLY)
    erg.ProcScan = TracingScan
    erg.acquire_session_lock = lambda sid: [dev]
    erg.keep_lock_for = lambda proc, fds: None
    erg.spawn_grok = lambda cmd, cwd, cols, rows: (FakeProc(), -1)
    erg.Live = FakeLive
    try:
        # A UUID-shaped id: session_holder only looks up those.
        erg.open_live("0f8e1d2c-1111-4222-8333-444455556666", ["grok"], "/", 80, 24)
    except RuntimeError:
        pass
    finally:
        erg.acquire_session_lock, erg.keep_lock_for, erg.spawn_grok = saved
        erg.ProcScan = real_scan
        erg.LIVE.clear()
        os.close(dev)
    if held:
        return "holder check: LIVE_LOCK was held while /proc/<pid>/fd was read (pids %s)" % held
    return None


def main():
    erg = load_module()
    failures = [f for f in (check_open_live(erg), check_resume_reply(erg),
                            check_failed_lock_kill(erg),
                            check_proc_scan_outside_lock(erg),
                            check_holder_fd_walk_outside_lock(erg)) if f]
    for failure in failures:
        print("FAIL: " + failure)
    if failures:
        return 1
    print("PASS: LIVE_LOCK stays free while log, reply and kill waits block")
    return 0


if __name__ == "__main__":
    sys.exit(main())
