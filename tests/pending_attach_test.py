"""A chat page opened while its grok is still spawning waits for it, then shows it running.

A spawn runs with LIVE_LOCK released, so while it is in flight the session has no
LIVE entry yet. Before, /chat and /term waited on LIVE_LOCK and then saw the grok.
Now they must wait for the spawn too, or the page shows "Ended" for a session
that starts a moment later and never connects.

Run: python3 -B tests/pending_attach_test.py
Stdlib only, no network, no real grok.
"""
import importlib.util
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = tempfile.mkdtemp(prefix="ergo-pending-attach-")
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
    # Holds the spawn in flight, as a cwd on a hung mount does.
    with open(PIDF, "w") as handle:
        handle.write(str(os.getpid()))
    os.open(FIFO, os.O_RDONLY)


def main():
    erg = load()
    real_popen = subprocess.Popen

    def blocking_popen(*args, **kwargs):
        kwargs["preexec_fn"] = block_before_exec
        return real_popen(*args, **kwargs)

    failures = []
    sid = str(uuid.uuid4())
    cmd = [sys.executable, "-c", "import time; time.sleep(60)", "--resume", sid]
    subprocess.Popen = blocking_popen
    spawn = threading.Thread(target=lambda: erg.open_live(sid, cmd, TMP, 80, 24), daemon=True)
    spawn.start()
    server = ThreadingHTTPServer(("127.0.0.1", 0), erg.Handler)
    server.token = None  # no login: the test is about attach, not auth
    server.tls = False
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        for _ in range(200):
            if os.path.exists(PIDF) and os.path.getsize(PIDF):
                break
            time.sleep(0.05)
        if not os.path.exists(PIDF):
            failures.append("the spawn never reached its pre-exec block")
            return report(failures)

        # The spawn publishes once the page request has asked about the session:
        # the fix waits in wait_for_spawn, the old code reads session_state.
        asked = threading.Event()
        for name in ("wait_for_spawn", "session_state"):
            real = getattr(erg, name, None)
            if real is not None:
                setattr(erg, name, lambda *a, _real=real, **k: (asked.set(), _real(*a, **k))[1])

        def release():
            asked.wait(10)
            os.close(os.open(FIFO, os.O_WRONLY))

        threading.Thread(target=release, daemon=True).start()
        url = "http://127.0.0.1:%d/chat?resume=%s" % (server.server_address[1], sid)
        with urllib.request.urlopen(url, timeout=15) as resp:
            body = resp.read().decode("utf-8", "replace")
        # running=True renders status "connecting" and connects on load.
        # running=False renders "Ended" and never connects, even after the grok starts.
        if '<span id="status">connecting</span>' not in body:
            failures.append("chat page opened during the spawn does not connect (shows Ended)")
    finally:
        subprocess.Popen = real_popen
        server.shutdown()
        server.server_close()
        spawn.join(5)
        with erg.LIVE_LOCK:
            live = erg.LIVE.pop(sid, None)
        if live is not None:
            live.proc.kill()
            # Wait for grok and for the reader: a daemon thread still printing
            # `term end` while the interpreter shuts down aborts it (#230).
            live.proc.wait()
            live.reader.join()
    return report(failures)


def report(failures):
    for failure in failures:
        print("FAIL: " + failure)
    if failures:
        return 1
    print("PASS: chat opened during a spawn waits for it and connects")
    return 0


if __name__ == "__main__":
    sys.exit(main())
