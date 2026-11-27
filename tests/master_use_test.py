"""No write, resize or read reaches a reused fd number after the PTY master closed (#227).

Live.write, Live.resize, Live.repaint and the reader take the master's number
and then make their syscall.  If shutdown (Quit, kill_all on stop or Settings
restart) closes the master in between and another file gets that number, the
syscall goes to that file.  Each check forces that order with a hook on the
syscall for the master's number only: the hook runs the close first, puts an
unrelated file (or PTY) at the number if it is free (what a reused number
looks like), then makes the real call with the number the caller took.  The
check then reads whether the unrelated file was touched and whether the
master's number was closed once every user finished.

Run: python3 -B tests/master_use_test.py [code-dir]
(code-dir: the checkout whose ergosphere.py to test; tests/ab/master_use.py
passes main's for the A side.)
Stdlib only, no network, no real grok: the PTY child is python.
"""
import errno
import fcntl
import importlib.util
import os
import pty
import select
import shutil
import struct
import sys
import tempfile
import termios
import threading
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = tempfile.mkdtemp(prefix="ergo-muse-")
os.environ["HOME"] = os.path.join(TMP, "home")
os.makedirs(os.environ["HOME"])
PLANT = os.path.join(TMP, "unrelated.txt")
SLEEP = [sys.executable, "-c", "import time; time.sleep(60)"]
HELLO = [sys.executable, "-c",
         "import sys, time; sys.stdout.write('hi'); sys.stdout.flush(); time.sleep(60)"]
WAIT = 10
PLANT_SIZE = (7, 9)  # rows, cols of the unrelated PTY


def load(code):
    spec = importlib.util.spec_from_file_location("ergosphere", Path(code) / "ergosphere.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


erg = load(sys.argv[1] if len(sys.argv) > 1 else ROOT)
erg.REPAINT_PAUSE = 0


def new_live(cmd):
    proc, master = erg.spawn_grok(cmd, TMP, 80, 24)
    return erg.Live(str(uuid.uuid4()), proc, master, TMP), master


def is_open(fd):
    try:
        fcntl.fcntl(fd, fcntl.F_GETFD)
        return True
    except OSError:
        return False


def winsize(fd):
    rows, cols, _, _ = struct.unpack("HHHH", fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8))
    return rows, cols


class Plant:
    """An unrelated file (kind "file") or PTY (kind "pty") at fd, if fd is free."""

    def __init__(self, kind):
        self.kind = kind
        self.fd = None
        self.other = None

    def put(self, fd):
        if is_open(fd):
            return  # the master's number was not released: nothing can reuse it
        if self.kind == "pty":
            self.other, src = pty.openpty()
            if self.other == fd:  # keep the other end off the number the slave goes to
                moved = os.dup(self.other)
                os.close(self.other)
                self.other = moved
            fcntl.ioctl(src, termios.TIOCSWINSZ, struct.pack("HHHH", PLANT_SIZE[0], PLANT_SIZE[1], 0, 0))
        else:
            with open(PLANT, "wb") as fh:
                fh.write(b"PLANTED\n")
            src = os.open(PLANT, os.O_RDWR)
        if src != fd:  # often the kernel already hands out that number
            os.dup2(src, fd, inheritable=False)
            os.close(src)
        self.fd = fd

    def problem(self):
        """None if nothing was planted, or the plant is untouched; else what happened."""
        if self.fd is None:
            return None
        try:
            if self.kind == "pty":
                if winsize(self.fd) != PLANT_SIZE:
                    return "unrelated PTY at fd %d was resized to %s" % (self.fd, winsize(self.fd))
                return None
            content = os.pread(self.fd, 64, 0)
            pos = os.lseek(self.fd, 0, os.SEEK_CUR)
            if content != b"PLANTED\n" or pos != 0:
                return "unrelated file at fd %d was used (now %r, offset %d)" % (self.fd, content, pos)
            return None
        finally:
            os.close(self.fd)
            if self.other is not None:
                os.close(self.other)


class Hook:
    """Wraps mod.name; the nth call whose first argument is fd runs before() first."""

    def __init__(self, mod, name, fd, before, nth=1):
        self.mod, self.name, self.fd, self.before, self.nth = mod, name, fd, before, nth
        self.real = getattr(mod, name)
        self.calls = 0
        self.lock = threading.Lock()

    def __call__(self, fd, *args):
        if fd == self.fd:
            with self.lock:
                self.calls += 1
                hit = self.calls == self.nth
            if hit:
                self.before()
        return self.real(fd, *args)

    def __enter__(self):
        setattr(self.mod, self.name, self)
        return self

    def __exit__(self, *exc):
        setattr(self.mod, self.name, self.real)


def result(live, fd, plant, what=""):
    """Plant untouched, and the master's number closed once nobody uses it."""
    bad = plant.problem()
    if bad:
        return bad
    if plant.fd is None and is_open(fd):
        os.close(fd)
        return "master fd %d still open after every user finished%s" % (fd, what)
    return None


def close_then_plant(live, fd, plant):
    def before():
        live.shutdown()
        plant.put(fd)
    return before


def quit_during_write():
    # Quit while a keystroke is between taking the fd and os.write.
    live, fd = new_live(SLEEP)
    plant = Plant("file")
    with Hook(os, "write", fd, close_then_plant(live, fd, plant)):
        live.write(b"typed")
    return result(live, fd, plant)


def quit_during_resize():
    # Quit while a resize is between taking the fd and the TIOCSWINSZ ioctl.
    live, fd = new_live(SLEEP)
    plant = Plant("pty")
    with Hook(fcntl, "ioctl", fd, close_then_plant(live, fd, plant)):
        live.resize(100, 30)
    return result(live, fd, plant)


def quit_during_repaint():
    # An attach repaints (two resizes); Quit lands before the second ioctl.
    live, fd = new_live(SLEEP)
    plant = Plant("pty")
    with Hook(fcntl, "ioctl", fd, close_then_plant(live, fd, plant), nth=2):
        live.repaint(100, 30)
    return result(live, fd, plant)


def quit_during_read():
    # grok printed; the reader took the fd; Quit closes before its os.read.
    live, fd = new_live(HELLO)
    plant = Plant("file")
    with Hook(os, "read", fd, close_then_plant(live, fd, plant)):
        live._read()
    if b"PLANTED" in live.buffer:
        plant.problem()
        return "reader read the unrelated file into the replay"
    return result(live, fd, plant)


def held(fd, mod, name):
    """A hook that parks the call on fd until go is set."""
    entered, go = threading.Event(), threading.Event()

    def before():
        entered.set()
        go.wait(WAIT)
    return Hook(mod, name, fd, before), entered, go


def second_tab_write_and_resize():
    # One tab types while another tab's resize runs; Quit lands during both.
    live, fd = new_live(SLEEP)
    plant = Plant("file")
    wh, wentered, wgo = held(fd, os, "write")
    rh, rentered, rgo = held(fd, fcntl, "ioctl")
    with wh, rh:
        tw = threading.Thread(target=live.write, args=(b"typed",), daemon=True)
        tr = threading.Thread(target=live.resize, args=(100, 30), daemon=True)
        tw.start()
        tr.start()
        if not (wentered.wait(WAIT) and rentered.wait(WAIT)):
            return "write and resize never both reached their syscall"
        live.shutdown()
        plant.put(fd)
        wgo.set()
        tw.join(WAIT)
        early = plant.fd is None and not is_open(fd)
        rgo.set()
        tr.join(WAIT)
    if tw.is_alive() or tr.is_alive():
        return "write or resize did not return"
    if early:
        return "master closed while the resize still held it"
    return result(live, fd, plant)


def server_stop_during_write():
    # SIGTERM: STOP set, kill_all shuts the Live down while a write is held.
    live, fd = new_live(SLEEP)
    plant = Plant("file")
    hook, entered, go = held(fd, os, "write")
    with erg.LIVE_LOCK:
        erg.LIVE[live.sid] = live
    with hook:
        tw = threading.Thread(target=live.write, args=(b"typed",), daemon=True)
        tw.start()
        try:
            if not entered.wait(WAIT):
                return "write never reached os.write"
            erg.STOP.set()
            erg.kill_all()
            plant.put(fd)
        finally:
            erg.STOP.clear()
            go.set()
        tw.join(WAIT)
    if tw.is_alive():
        return "write did not return"
    return result(live, fd, plant)


def write_fails_mid_way():
    # Quit, then the write fails (EIO): the number is still closed, once.
    live, fd = new_live(SLEEP)
    plant = Plant("file")

    def before():
        live.shutdown()
        plant.put(fd)
        raise OSError(errno.EIO, "pty write failed")
    with Hook(os, "write", fd, before):
        try:
            live.write(b"typed")
        except OSError as exc:
            plant.problem()
            return "write raised after Quit: %s" % exc
    return result(live, fd, plant)


def quit_during_blocked_write():
    # A paste fills the PTY (grok is not reading) and os.write blocks; Quit
    # must not wait for it, and the number closes once the write gives up.
    live, fd = new_live(SLEEP)
    tw = threading.Thread(target=live.write, args=(b"x" * (1 << 20),), daemon=True)
    tw.start()
    poller = select.poll()
    poller.register(fd, select.POLLOUT)
    deadline = erg.time.monotonic() + WAIT
    while poller.poll(0) and erg.time.monotonic() < deadline:
        tw.join(0.01)
    if poller.poll(0):
        return "the PTY never filled"
    ts = threading.Thread(target=live.shutdown, daemon=True)
    ts.start()
    ts.join(WAIT)
    if ts.is_alive():
        return "shutdown hung behind the blocked write"
    tw.join(WAIT)
    if tw.is_alive():
        return "write still blocked after shutdown"
    return result(live, fd, Plant("file"))


CHECKS = (
    quit_during_write,
    quit_during_resize,
    quit_during_repaint,
    quit_during_read,
    second_tab_write_and_resize,
    server_stop_during_write,
    write_fails_mid_way,
    quit_during_blocked_write,
)


def main():
    failed = 0
    try:
        for check in CHECKS:
            try:
                problem = check()
            except Exception as exc:
                problem = "raised %s: %s" % (type(exc).__name__, exc)
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
