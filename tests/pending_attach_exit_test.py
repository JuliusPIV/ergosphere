"""tests/pending_attach_test.py exits 0 when a Live reader thread prints at shutdown (#230).

The test kills its grok and returns. If the session's reader thread (a daemon)
then prints `term end` while the interpreter is finalizing, CPython aborts
(rc 134, "could not acquire lock for <stdout> at interpreter shutdown"). Under
load that happened by chance. Here the order is forced: the reader's `term end`
print waits until shutdown has started (atexit), unless someone join()s that
thread, and stdout is a full pipe, so the write is still in flight when CPython
finalizes. A test that waits for its grok and its reader before returning is
unaffected; one that does not aborts every time.

Run: python3 -B tests/pending_attach_exit_test.py
Stdlib only, no network, no real grok. Linux (/proc/self/task).
"""
import atexit
import builtins
import os
import runpy
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "tests" / "pending_attach_test.py"


def write_in_flight():
    # A thread of this process blocked in write(2) on fd 1.
    for tid in os.listdir("/proc/self/task"):
        try:
            with open("/proc/self/task/%s/syscall" % tid) as handle:
                parts = handle.read().split()
        except OSError:
            continue
        if len(parts) > 1 and parts[0] == "1" and int(parts[1], 16) == 1:
            return True
    return False


def readers_alive():
    return [t for t in threading.enumerate() if t.name.endswith("(_read)") and t.is_alive()]


def forced(script):
    real_print = builtins.print
    real_join = threading.Thread.join
    cond = threading.Condition()
    state = {"go": False, "joined": set()}

    def held_print(*args, **kwargs):
        me = threading.current_thread()
        if args and str(args[0]).startswith("term end") and me is not threading.main_thread():
            with cond:
                cond.wait_for(lambda: state["go"] or me in state["joined"])
        return real_print(*args, **kwargs)

    def noted_join(self, timeout=None):
        # Whoever waits for a thread must not wait on our hold.
        with cond:
            state["joined"].add(self)
            cond.notify_all()
        return real_join(self, timeout)

    def at_shutdown():
        # atexit: after the script returned and stdout was flushed, before
        # CPython finalizes. Fill the stdout pipe, then release the reader.
        sys.stdout.flush()
        os.set_blocking(1, False)
        try:
            while True:
                os.write(1, b"." * 4096)
        except BlockingIOError:
            pass
        os.set_blocking(1, True)
        with cond:
            state["go"] = True
            cond.notify_all()
        end = time.monotonic() + 10
        while time.monotonic() < end and readers_alive() and not write_in_flight():
            time.sleep(0.01)

    builtins.print = held_print
    threading.Thread.join = noted_join
    atexit.register(at_shutdown)
    sys.argv = [script]
    runpy.run_path(script, run_name="__main__")


def main():
    read_end, write_end = os.pipe()
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen([sys.executable, "-B", __file__, "--forced", str(TARGET)],
                                stdout=write_end, stderr=err, cwd=tempfile.gettempdir())
        os.close(write_end)
        # Read stdout only once the child is gone: the child fills the pipe on purpose.
        try:
            rc = proc.wait(timeout=120)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            rc = "timeout"
        with os.fdopen(read_end, "rb") as handle:
            out = handle.read().decode("utf-8", "replace")
        err.seek(0)
        stderr = err.read().decode("utf-8", "replace")
    failures = []
    if "PASS:" not in out:
        failures.append("pending_attach_test.py did not print PASS (rc=%s): %s" % (rc, stderr.strip()[-300:]))
    elif rc != 0:
        fatal = [line for line in stderr.splitlines() if line.startswith("Fatal Python error")]
        failures.append("pending_attach_test.py printed PASS but exited %s with a reader thread printing "
                        "at shutdown: %s" % (rc, fatal[0] if fatal else stderr.strip()[-300:]))
    for failure in failures:
        print("FAIL: " + failure)
    if failures:
        return 1
    print("PASS: pending_attach_test.py exits 0 when its reader thread would print at shutdown")
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--forced":
        forced(sys.argv[2])
    else:
        sys.exit(main())
