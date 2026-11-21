"""An attach that races another tab's start attaches, never "ended" (issue #45 review).

A request passes wait_for_spawn while the session is stopped.  Then another
tab's Resume puts the id in STARTING, and the request reads the session as
starting with nothing here yet.  It must wait for that start and attach, as
on main where it waited on LIVE_LOCK:
  chat: GET /chat renders "connecting", not Ended;
  term: /term sends the reattach "session" frame, not "ended";
  holder: GET /holder answers here=true, not here=false (the hold page shows Ended).
The order is forced: the request's first session_state waits until the other
start is spawning, and the spawn is released only once the request has
answered or waits for it.

Run: python3 -B tests/attach_race_test.py
Stdlib only, no network, no real grok.
"""
import base64
import contextlib
import importlib.util
import io
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import urllib.request
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = tempfile.mkdtemp(prefix="ergo-attach-race-")
os.environ["HOME"] = TMP
WAIT = 10


def load():
    spec = importlib.util.spec_from_file_location("ergosphere", ROOT / "ergosphere.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def get(port, path):
    with urllib.request.urlopen("http://127.0.0.1:%d%s" % (port, path), timeout=WAIT * 2) as resp:
        return resp.read().decode("utf-8", "replace")


def chat(port, sid):
    body = get(port, "/chat?resume=" + sid)
    return "connecting" if '<span id="status">connecting</span>' in body else "Ended"


def holder(port, sid):
    return "here=%s" % json.loads(get(port, "/holder?id=" + sid)).get("here")


def term(port, sid):
    origin = "http://127.0.0.1:%d" % port
    sock = socket.create_connection(("127.0.0.1", port), timeout=WAIT * 2)
    try:
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall(("GET /term?resume=%s HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nOrigin: %s\r\n"
                      "Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
                      "Sec-WebSocket-Version: 13\r\n\r\n" % (sid, port, origin, key)).encode())
        reader = sock.makefile("rb")
        status = reader.readline()
        if b" 101 " not in status:
            return "no upgrade: %r" % status
        while reader.readline() not in (b"\r\n", b""):
            pass
        while True:
            head = reader.read(2)
            if len(head) < 2:
                return "closed"
            n = head[1] & 0x7F
            if n == 126:
                n = int.from_bytes(reader.read(2), "big")
            elif n == 127:
                n = int.from_bytes(reader.read(8), "big")
            payload = reader.read(n)
            if head[0] & 0x0F == 1:
                return json.loads(payload).get("type")
    finally:
        sock.close()


CASES = [("chat", chat, "connecting"), ("term", term, "session"), ("holder", holder, "here=True")]


def run_case(erg, port, name, probe, want):
    sid = str(uuid.uuid4())
    cmd = [sys.executable, "-c", "import time; time.sleep(60)", sid]
    asked, started, rewait = threading.Event(), threading.Event(), threading.Event()
    spawning, release = threading.Event(), threading.Event()
    calls = []
    real_state, real_wait, real_spawn = erg.session_state, erg.wait_for_spawn, erg.spawn_grok

    def session_state(s, *a, **k):
        if s == sid and not calls:
            calls.append(s)
            asked.set()
            started.wait(WAIT)
        return real_state(s, *a, **k)

    def wait_for_spawn(s):
        if s == sid and asked.is_set():
            rewait.set()
        return real_wait(s)

    def spawn(c, cwd, cols, rows):
        spawning.set()
        release.wait(WAIT)
        return real_spawn(c, cwd, cols, rows)

    erg.session_state, erg.wait_for_spawn, erg.spawn_grok = session_state, wait_for_spawn, spawn
    box = {}

    def ask():
        try:
            box["got"] = probe(port, sid)
        except Exception as exc:
            box["got"] = "%s: %s" % (type(exc).__name__, exc)

    asker = threading.Thread(target=ask, daemon=True)
    opener = threading.Thread(target=lambda: erg.open_live(sid, cmd, TMP, 80, 24), daemon=True)
    try:
        asker.start()
        if not asked.wait(WAIT):
            return "%s: the request never read the session state" % name
        opener.start()
        if not spawning.wait(WAIT):
            return "%s: the other tab's start never began its spawn" % name
        started.set()
        for _ in range(WAIT * 20):
            if not asker.is_alive() or rewait.wait(0.05):
                break
        release.set()
        asker.join(WAIT * 2)
        opener.join(WAIT)
    finally:
        started.set()
        release.set()
        erg.session_state, erg.wait_for_spawn, erg.spawn_grok = real_state, real_wait, real_spawn
        erg.kill_all()
    if box.get("got") != want:
        return "%s: an attach that raced another tab's start got %r, not %r" % (name, box.get("got"), want)
    return None


def main():
    erg = load()
    erg.find_session = lambda s: {"id": s, "cwd": TMP, "title": ""}
    server = ThreadingHTTPServer(("127.0.0.1", 0), erg.Handler)
    server.token = None
    server.tls = False
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    only = sys.argv[1:] or [c[0] for c in CASES]
    failures = []
    with contextlib.redirect_stdout(io.StringIO()):
        try:
            for name, probe, want in CASES:
                if name in only:
                    failure = run_case(erg, port, name, probe, want)
                    if failure:
                        failures.append(failure)
        finally:
            server.shutdown()
            server.server_close()
            shutil.rmtree(TMP, ignore_errors=True)
    for failure in failures:
        print("FAIL: " + failure)
    if failures:
        return 1
    print("PASS: chat, term and holder attach to a start that raced them: %s" % ", ".join(only))
    return 0


if __name__ == "__main__":
    sys.exit(main())
