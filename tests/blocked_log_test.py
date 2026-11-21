"""A blocked log write does not stall the request that writes it (issue #70).

1. A real server whose stdout/stderr pipe is full: a launch, a reattach and
   /health still answer, and once the pipe is read the log lines arrive
   (tests/ab/blocked_log.py run).
2. The queued log itself, in process: a full queue drops lines and the log
   says how many; a write that fails does not stop the writer; two threads'
   print() lines never interleave; drain_log gives up after its timeout when
   the log stays blocked.

Run: [ERGOSPHERE_TEST_PORT=<spare port>] python3 -B tests/blocked_log_test.py
(a free port from the OS when unset)
Stdlib only, no network, no real grok.
"""
import importlib.util
import os
import queue
import shutil
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WAIT = 5


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Stream:
    """Records writes. block: the first write waits on it. fail: the first write raises."""

    def __init__(self, block=None, fail=False):
        self.text = ""
        self.block = block
        self.fail = fail
        self.entered = threading.Event()

    def write(self, text):
        self.entered.set()
        if self.block is not None:
            block, self.block = self.block, None
            block.wait(WAIT)
        if self.fail:
            self.fail = False
            raise OSError(5, "Input/output error")
        self.text += text
        return len(text)

    def flush(self):
        pass


def fresh_writer(erg, size):
    erg.LOG_QUEUE = queue.Queue(size)
    erg.QueuedLog.dropped = 0
    threading.Thread(target=erg._log_writer, daemon=True).start()


def wait_for(fn):
    end = time.monotonic() + WAIT
    while time.monotonic() < end:
        if fn():
            return True
        time.sleep(0.01)
    return False


def check_drops(erg):
    release = threading.Event()
    stream = Stream(block=release)
    fresh_writer(erg, 3)
    out = erg.QueuedLog(stream)
    out.write("first\n")
    if not stream.entered.wait(WAIT):
        return "the writer never wrote the first line"
    for i in range(5):  # 3 fit, 2 are dropped
        out.write("line %d\n" % i)
    release.set()
    want = "first\nlog: dropped 2 lines while the log was blocked\nline 0\nline 1\nline 2\n"
    if not wait_for(lambda: stream.text == want):
        return "full queue: wrote %r, want %r" % (stream.text, want)
    return None


def check_write_error(erg):
    stream = Stream(fail=True)
    fresh_writer(erg, 10)
    out = erg.QueuedLog(stream)
    out.write("lost\n")
    out.write("kept\n")
    want = "log: dropped 1 lines while the log was blocked\nkept\n"
    if not wait_for(lambda: stream.text == want):
        return "after a failed write: wrote %r, want %r" % (stream.text, want)
    return None


def check_no_interleave(erg):
    stream = Stream()
    fresh_writer(erg, 10000)
    out = erg.QueuedLog(stream)

    def talk(n):
        for i in range(300):
            print("thread %d line %d" % (n, i), file=out, flush=True)

    workers = [threading.Thread(target=talk, args=(n,)) for n in range(8)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    if not wait_for(lambda: stream.text.count("\n") == 2400):
        return "only %d of 2400 lines written" % stream.text.count("\n")
    bad = [l for l in stream.text.splitlines() if not l.startswith("thread ") or len(l.split()) != 4]
    if bad:
        return "lines interleaved: %r" % bad[:3]
    return None


def check_drain_gives_up(erg):
    release = threading.Event()
    stream = Stream(block=release)
    fresh_writer(erg, 10)
    saved = sys.stdout, sys.stderr
    sys.stdout = sys.stderr = erg.QueuedLog(stream)
    try:
        print("stuck", flush=True)
        began = time.monotonic()
        done = erg.drain_log(timeout=0.5)
        took = time.monotonic() - began
    finally:
        sys.stdout, sys.stderr = saved
        release.set()
    if done or took > 2:
        return "drain_log with a blocked log: returned %r after %.1fs" % (done, took)
    return None


def main():
    failures = []
    port = os.environ.get("ERGOSPHERE_TEST_PORT")
    if not port:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = str(s.getsockname()[1])
    ab = load("blocked_log", ROOT / "tests" / "ab" / "blocked_log.py")
    work = tempfile.mkdtemp(prefix="ergo-blocked-log-")
    try:
        result = ab.run(str(ROOT), port, work)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    for name in ab.ORDER:
        c = result["checks"].get(name) or {"ok": False, "value": "not run"}
        if not c["ok"]:
            failures.append("%s: %s" % (name, c["value"]))
    erg = load("ergosphere", ROOT / "ergosphere.py")
    if not hasattr(erg, "QueuedLog"):
        failures.append("ergosphere.py has no queued log (QueuedLog)")
    else:
        for fn in (check_drops, check_write_error, check_no_interleave, check_drain_gives_up):
            failure = fn(erg)
            if failure:
                failures.append(failure)
    for failure in failures:
        print("FAIL: " + failure)
    if failures:
        return 1
    print("PASS: launch, reattach and /health answer while the log is blocked; the queued log drops, counts and drains")
    return 0


if __name__ == "__main__":
    sys.exit(main())
