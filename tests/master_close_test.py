"""A Live's PTY master is closed once, never a second time (#218).

shutdown() (Quit, kill_all on stop or Settings restart) and the reader's exit
both close the master.  If a file got the same fd number in between, a second
close closes that file instead.  Each check forces one order, puts an
unrelated file at the master's old number with dup2 (what a reused number
looks like), runs the other closer, and checks that file is still open and
untouched.

Run: python3 -B tests/master_close_test.py [code-dir]
(code-dir: the checkout whose ergosphere.py to test; tests/ab/master_close.py
passes main's for the A side.)
Stdlib only, no network, no real grok: the PTY child is python.
"""
import importlib.util
import os
import shutil
import sys
import tempfile
import threading
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = tempfile.mkdtemp(prefix="ergo-mclose-")
os.environ["HOME"] = os.path.join(TMP, "home")
os.makedirs(os.environ["HOME"])
PLANT = os.path.join(TMP, "unrelated.txt")
with open(PLANT, "wb") as fh:
    fh.write(b"PLANTED\n")
SLEEP = [sys.executable, "-c", "import time; time.sleep(60)"]
QUICK = [sys.executable, "-c", "pass"]
WAIT = 10


def load(code):
    spec = importlib.util.spec_from_file_location("ergosphere", Path(code) / "ergosphere.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


erg = load(sys.argv[1] if len(sys.argv) > 1 else ROOT)


def new_live(cmd):
    proc, master = erg.spawn_grok(cmd, TMP, 80, 24)
    sid = str(uuid.uuid4())
    return erg.Live(sid, proc, master, TMP), master


def plant(fd):
    """An unrelated file now holds the master's old number."""
    src = os.open(PLANT, os.O_RDONLY)
    if src != fd:  # often the kernel already hands out that number
        os.dup2(src, fd, inheritable=False)
        os.close(src)


def planted_ok(fd):
    """None if fd is still the planted file, unread; else what went wrong."""
    try:
        st = os.fstat(fd)
    except OSError as exc:
        return "fd %d closed: %s" % (fd, exc)
    if st.st_ino != os.stat(PLANT).st_ino:
        return "fd %d is not the planted file" % fd
    pos = os.lseek(fd, 0, os.SEEK_CUR)
    os.close(fd)
    if pos != 0:
        return "planted file was read (offset %d)" % pos
    return None


def quit_then_reader():
    # Quit (or SIGTERM right after a launch) before the reader ran its exit.
    live, fd = new_live(SLEEP)
    live.shutdown()
    plant(fd)
    live._read()
    return planted_ok(fd)


def grok_exit_then_shutdown():
    # grok exits, the reader closes; later kill_all (stop, Settings restart)
    # or a late Quit still calls shutdown on that Live.
    live, fd = new_live(QUICK)
    live._read()
    plant(fd)
    live.shutdown()
    return planted_ok(fd)


def quit_twice():
    # Two tabs press Quit: two shutdown calls on one Live.
    live, fd = new_live(SLEEP)
    live.shutdown()
    plant(fd)
    live.shutdown()
    return planted_ok(fd)


def server_stop_then_reader():
    # SIGTERM: STOP set, kill_all shuts the Live down, its reader exits on STOP.
    live, fd = new_live(SLEEP)
    with erg.LIVE_LOCK:
        erg.LIVE[live.sid] = live
    erg.STOP.set()
    try:
        erg.kill_all()
        plant(fd)
        live._read()
    finally:
        erg.STOP.clear()
    return planted_ok(fd)


class HeldPoller:
    """The reader's poll: the first call waits until the test lets it go."""

    def __init__(self, entered, go):
        self.entered = entered
        self.go = go
        self.fd = None
        self.calls = 0

    def register(self, fd, mask):
        self.fd = fd

    def poll(self, timeout):
        self.calls += 1
        if self.calls == 1:
            self.entered.set()
            self.go.wait(WAIT)
            # What poll reports once its fd is closed or reused: an event.
            return [(self.fd, 1)]
        return []


def reader_blocked_in_poll():
    # The reader sits in poll() while shutdown closes the master and a file
    # takes the number; then poll wakes it.
    live, fd = new_live(SLEEP)
    entered, go = threading.Event(), threading.Event()
    real_poll = erg.select.poll
    erg.select.poll = lambda: HeldPoller(entered, go)
    try:
        reader = threading.Thread(target=live._read, daemon=True)
        reader.start()
        if not entered.wait(WAIT):
            return "reader never reached poll"
        live.shutdown()
        plant(fd)
        go.set()
        reader.join(WAIT)
        if reader.is_alive():
            return "reader did not exit"
    finally:
        erg.select.poll = real_poll
    if b"PLANTED" in live.buffer:
        return "reader read the planted file into the replay"
    return planted_ok(fd)


def resize_after_close():
    # repaint's second resize runs after a shutdown or the reader's exit closed
    # the master; the -1 left behind must be a no-op, not an escaped error.
    live, fd = new_live(SLEEP)
    live.shutdown()
    try:
        live.repaint(80, 24)
        live.resize(100, 30)
    except Exception as exc:
        return "resize after close raised %s: %s" % (type(exc).__name__, exc)
    return None


CHECKS = (
    quit_then_reader,
    grok_exit_then_shutdown,
    quit_twice,
    server_stop_then_reader,
    reader_blocked_in_poll,
    resize_after_close,
)


def main():
    failed = 0
    try:
        for check in CHECKS:
            problem = check()
            print("%s %s%s" % ("FAIL" if problem else "ok", check.__name__, ": " + problem if problem else ""))
            failed += 1 if problem else 0
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    if failed:
        print("FAIL: %d of %d" % (failed, len(CHECKS)))
        return 1
    print("PASS: %d checks" % len(CHECKS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
