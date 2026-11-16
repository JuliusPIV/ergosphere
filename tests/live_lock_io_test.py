"""No log write, HTTP reply or /proc read happens while LIVE_LOCK is held.

A stalled stdout (log on a slow disk), a slow client or a /proc walk on a
busy box must not hold the lock that every launch, resume and reattach needs.

Run: python3 -B tests/live_lock_io_test.py
Stdlib only, no network, no grok, no server.
"""
import contextlib
import importlib.util
import io
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
        self.buffer = bytearray(b"output")
        self.trimmed = False
        self.closed = False

    def alive(self):
        return True

    def start(self):
        pass


@contextlib.contextmanager
def fake_grok(erg):
    """open_live starts no process; the real spawn_grok and Live come back after."""
    saved = (erg.spawn_grok, erg.Live)
    erg.spawn_grok = lambda cmd, cwd, cols, rows: (FakeProc(), -1)
    erg.Live = FakeLive
    try:
        yield
    finally:
        erg.spawn_grok, erg.Live = saved
        erg.LIVE.clear()


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

    real = sys.stdout
    with fake_grok(erg):
        sys.stdout = SlowStdout()
        try:
            return lock_free_during(
                erg, "open_live log line", entered, release,
                lambda: erg.open_live("sid-open", ["grok"], "/", 80, 24))
        finally:
            sys.stdout = real


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


def check_proc_reads_outside_lock(erg):
    """open_live looks for another grok on the session by reading /proc: not under LIVE_LOCK."""
    held = []
    real_scan = erg.ProcScan

    class TracingScan(real_scan):
        def __init__(self):
            if erg.LIVE_LOCK.locked():
                held.append("/proc")
            real_scan.__init__(self)
            self.groks = [1]  # pid 1 stands in for a grok, so its open files are looked at
            self.cmd[1] = b"grok\0"

        def fd_targets(self, pid):
            if erg.LIVE_LOCK.locked():
                held.append("/proc/%d/fd" % pid)
            return real_scan.fd_targets(self, pid)

    erg.ProcScan = TracingScan
    try:
        with fake_grok(erg), contextlib.redirect_stdout(io.StringIO()):
            # A UUID-shaped id: session_holder only looks up those.
            erg.open_live("0f8e1d2c-1111-4222-8333-444455556666", ["grok"], "/", 80, 24)
    finally:
        erg.ProcScan = real_scan
    if held:
        return "holder check: LIVE_LOCK was held while %s was read" % ", ".join(sorted(set(held)))
    return None


def main():
    erg = load_module()
    failures = [f for f in (check_open_live(erg), check_resume_reply(erg),
                            check_proc_reads_outside_lock(erg)) if f]
    for failure in failures:
        print("FAIL: " + failure)
    if failures:
        return 1
    print("PASS: LIVE_LOCK stays free while log and reply writes block and /proc is read")
    return 0


if __name__ == "__main__":
    sys.exit(main())
