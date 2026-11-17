"""A /term client that stops reading cannot grow server memory without limit.

A stand-in grok streams output as fast as the PTY takes it.  One attached
client never finishes a write (a frozen tab); another reads normally.  The
frozen client's queued output must stay bounded and its socket must be shut
down, so the page reconnects and resyncs from the replay.  The reading client
and grok carry on, and a new attach still gets the replay.

Run: python3 -B tests/slow_client_test.py
Stdlib only, no network, no real grok: "grok" is python under that name.
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = tempfile.mkdtemp(prefix="ergo-slow-")
HOME = os.path.join(TMP, "home")
BIN = os.path.join(TMP, "bin")
GROK = os.path.join(BIN, "grok")
CWD = os.path.join(HOME, "proj")
os.makedirs(BIN)
os.makedirs(CWD)
os.symlink(sys.executable, GROK)
os.environ["HOME"] = HOME
FLOOD = "import os\nwhile True: os.write(1, b'x' * 65536)\n"
# Far above any sane per-client cap, far below what an unbounded queue reaches.
BOUND = 16 * 1024 * 1024


def load():
    spec = importlib.util.spec_from_file_location("ergosphere", ROOT / "ergosphere.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def new_sid():
    sid = "00000051-" + str(uuid.uuid4())[9:]
    folder = os.path.join(HOME, ".grok", "sessions", CWD.replace("/", "%2F"), sid)
    os.makedirs(folder)
    with open(os.path.join(folder, "summary.json"), "w") as fh:
        json.dump({"id": sid, "cwd": CWD, "title": "flood"}, fh)
    return sid


class FrozenTab:
    """A tab that stopped reading: a write blocks until the socket is shut down."""

    def __init__(self):
        self.shut = threading.Event()
        self.connection = self

    def shutdown(self, how):
        self.shut.set()

    def _send_frame(self, opcode, payload):
        self.shut.wait()
        raise OSError("broken pipe")


class ReadingTab:
    def __init__(self):
        self.bytes = 0
        self.connection = self
        self.shut = threading.Event()

    def shutdown(self, how):
        self.shut.set()

    def _send_frame(self, opcode, payload):
        if opcode == 2:
            self.bytes += len(payload)


def queued_bytes(client):
    with client.queue.mutex:
        return sum(len(item[1]) for item in client.queue.queue if item)


def run(erg):
    sid = new_sid()
    live, _ = erg.open_live(sid, [GROK, "-c", FLOOD, "--resume", sid], CWD, 80, 24)
    frozen, reading = FrozenTab(), ReadingTab()
    fc = live.attach(frozen)
    rc = live.attach(reading)
    peak = 0
    end = time.time() + 8
    while time.time() < end and not frozen.shut.is_set():
        peak = max(peak, queued_bytes(fc))
        if peak > BOUND:
            break
        time.sleep(0.02)
    peak = max(peak, queued_bytes(fc))
    failures = []
    if peak > BOUND:
        failures.append("frozen client queued %d bytes (over %d): unbounded" % (peak, BOUND))
    if not frozen.shut.is_set():
        failures.append("frozen client's socket was not shut down")
    time.sleep(0.5)
    if queued_bytes(fc) > BOUND:
        failures.append("frozen client still holds %d queued bytes" % queued_bytes(fc))
    if not rc.alive or reading.shut.is_set():
        failures.append("the reading client was dropped too")
    before = reading.bytes
    time.sleep(0.5)
    if reading.bytes <= before:
        failures.append("the reading client stopped getting output")
    if not live.alive():
        failures.append("grok did not keep running")
    late = ReadingTab()
    live.attach(late)
    time.sleep(0.3)
    if late.bytes == 0:
        failures.append("a new attach after the drop got no replay")
    live.shutdown()
    return failures, peak


def main():
    erg = load()
    erg.print = lambda *a, **k: None
    try:
        failures, peak = run(erg)
    finally:
        erg.STOP.set()
        for live in list(erg.LIVE.values()):
            erg.kill_proc(live.proc)
        shutil.rmtree(TMP, ignore_errors=True)
    for failure in failures:
        print("FAIL: " + failure)
    if failures:
        return 1
    print("PASS: frozen client dropped at %d queued bytes; reader, grok and replay unaffected" % peak)
    return 0


if __name__ == "__main__":
    sys.exit(main())
