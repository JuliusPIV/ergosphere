"""A new session whose grok exits before it saved the session is "unsaved" (#44).

grok writes a new session's summary.json about 0.2 s after it starts.  A grok
that exits (or is quit, or the server stops) before that leaves nothing to
resume: /resume could only answer "unknown session", for ever.  Each case
below checks that every reply that could offer Resume says "unsaved" instead:
the ended frame (every tab, and a late attach), a reload of /chat, /resume.
Cases (one fresh session id each), with the interleaving each covers:
  exit       grok exits on its own before saving (start)
  saved      grok saves, then exits: plain Ended, Resume still works
  quit       a tab's Quit before grok saved (stop)
  stop       the server stops (kill_all, SIGTERM and Settings restart path);
             afterwards nothing counts as running
  tabs       two tabs attached, and a late attach after the ended frame
  half       grok died while writing summary.json (failure mid-way)
  resumes    two /resume requests at once: both unsaved, no grok started
  slowscan   a slow sessions-folder scan at exit holds no lock
  starting   /chat while the new session is still spawning waits for it,
             never says unsaved

Run: python3 -B tests/unsaved_session_test.py [case ...]
Stdlib only, no network, no real grok: "grok" is a python child that waits
for a control file.
"""
import base64
import contextlib
import importlib.util
import io
import json
import os
import shutil
import socket
import struct
import sys
import tempfile
import threading
import time
import types
import urllib.request
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = tempfile.mkdtemp(prefix="ergo-unsaved-")
os.environ["HOME"] = TMP
WAIT = 10
SESSIONS = os.path.join(TMP, ".grok", "sessions", "t")

# The stand-in grok: prints once, then waits for its control file.
# "exit": exit now.  "save": write summary.json, then exit.
# "half": write half a summary.json, then exit.
GROK = r"""
import json, os, sys, time
ctl, sid, folder, cwd = sys.argv[1:5]
sys.stdout.write("fake grok ready\r\n"); sys.stdout.flush()
while True:
    try:
        what = open(ctl).read().strip()
    except OSError:
        what = ""
    if what in ("save", "half"):
        os.makedirs(folder, exist_ok=True)
        text = json.dumps({"info": {"id": sid, "cwd": cwd}, "generated_title": "t",
                           "updated_at": "2026-01-01T00:00:00Z"})
        with open(os.path.join(folder, "summary.json"), "w") as f:
            f.write(text if what == "save" else text[:len(text) // 2])
    if what:
        sys.exit(0)
    time.sleep(0.02)
"""


def load():
    spec = importlib.util.spec_from_file_location("ergosphere", ROOT / "ergosphere.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def wait_for(fn):
    end = time.monotonic() + WAIT
    while time.monotonic() < end:
        if fn():
            return True
        time.sleep(0.02)
    return False


class Tab:
    """A /term WebSocket client that collects the server's text frames."""

    def __init__(self, port, sid):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=WAIT)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(("GET /term?resume=%s HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n"
                           "Origin: http://127.0.0.1:%d\r\nUpgrade: websocket\r\n"
                           "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
                           "Sec-WebSocket-Version: 13\r\n\r\n" % (sid, port, port, key)).encode())
        self.reader = self.sock.makefile("rb")
        self.status = self.reader.readline()
        while self.reader.readline() not in (b"\r\n", b""):
            pass
        self.texts = []
        self.lock = threading.Lock()
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        try:
            while True:
                head = self.reader.read(2)
                if len(head) < 2:
                    return
                n = head[1] & 0x7F
                if n == 126:
                    n = int.from_bytes(self.reader.read(2), "big")
                elif n == 127:
                    n = int.from_bytes(self.reader.read(8), "big")
                payload = self.reader.read(n)
                if head[0] & 0x0F == 1:
                    with self.lock:
                        self.texts.append(json.loads(payload))
        except (OSError, ValueError):
            return

    def frame(self, kind):
        with self.lock:
            for msg in self.texts:
                if msg.get("type") == kind:
                    return msg
        return None

    def send_text(self, text):
        data = text.encode()
        mask = os.urandom(4)
        body = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(struct.pack("!BB", 0x81, 0x80 | len(data)) + mask + body)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def chat_unsaved(port, sid):
    with urllib.request.urlopen("http://127.0.0.1:%d/chat?resume=%s" % (port, sid), timeout=WAIT * 2) as resp:
        body = resp.read().decode("utf-8", "replace")
    if "var unsaved = true;" in body:
        return "unsaved"
    if '<span id="status">connecting</span>' in body:
        return "connecting"
    return "Ended"


def resume(erg, sid):
    replies = []
    handler = types.SimpleNamespace(_json_object=lambda: {"id": sid},
                                    _json=lambda code, body: replies.append((code, body)))
    erg.Handler._resume(handler)
    return replies[0] if replies else (None, {})


class Case:
    def __init__(self, erg, port):
        self.erg, self.port = erg, port
        self.sid = str(uuid.uuid4())
        self.ctl = os.path.join(TMP, "ctl-" + self.sid)
        self.folder = os.path.join(SESSIONS, self.sid)
        self.tabs = []

    def start(self):
        cmd = [sys.executable, "-c", GROK, self.ctl, self.sid, self.folder, TMP]
        live, _ = self.erg.open_live(self.sid, cmd, TMP, 80, 24)
        self.live = live
        return live

    def tab(self):
        tab = Tab(self.port, self.sid)
        self.tabs.append(tab)
        if not wait_for(lambda: tab.frame("session")):
            raise AssertionError("tab did not attach: %r %r" % (tab.status, tab.texts))
        return tab

    def tell(self, what):
        with open(self.ctl + ".tmp", "w") as f:
            f.write(what)
        os.replace(self.ctl + ".tmp", self.ctl)

    def gone(self):
        # The reader popped LIVE: the session is stopped.
        return wait_for(lambda: self.erg.LIVE.get(self.sid) is None and self.live.proc.poll() is not None)

    def ended(self, tab):
        if not wait_for(lambda: tab.frame("ended")):
            return "no ended frame: %r" % tab.texts
        return tab.frame("ended")

    def close(self):
        for tab in self.tabs:
            tab.close()


def expect_unsaved_after(c, tab):
    msg = c.ended(tab)
    if not isinstance(msg, dict) or msg.get("unsaved") is not True:
        return "ended frame does not say unsaved: %r" % (msg,)
    if not c.gone():
        return "grok still in LIVE"
    got = chat_unsaved(c.port, c.sid)
    if got != "unsaved":
        return "reloaded /chat shows %s, not unsaved" % got
    code, body = resume(c.erg, c.sid)
    if code != 404 or body.get("unsaved") is not True or body.get("state") != "unsaved":
        return "/resume answered %s %r, not 404 unsaved" % (code, body)
    return None


def case_exit(c):
    c.start()
    tab = c.tab()
    c.tell("exit")
    return expect_unsaved_after(c, tab)


def case_saved(c):
    c.start()
    tab = c.tab()
    c.tell("save")
    msg = c.ended(tab)
    if not isinstance(msg, dict) or "unsaved" in msg:
        return "a saved session's ended frame: %r" % (msg,)
    if not c.gone():
        return "grok still in LIVE"
    got = chat_unsaved(c.port, c.sid)
    if got != "Ended":
        return "a saved session's /chat shows %s, not Ended" % got
    os.remove(c.ctl)
    code, body = resume(c.erg, c.sid)
    c.erg.kill_all()
    if code != 200:
        return "a saved session's /resume answered %s %r, not 200" % (code, body)
    return None


def case_quit(c):
    # The quitting tab's own socket closes at once (its page shows Ended on
    # close and learns unsaved from /resume); the other tab gets the frame.
    c.start()
    quitter, other = c.tab(), c.tab()
    quitter.send_text(json.dumps({"type": "stop"}))
    return expect_unsaved_after(c, other)


def case_stop(c):
    c.start()
    tab = c.tab()
    c.erg.kill_all()
    fail = expect_unsaved_after(c, tab)
    if fail:
        return fail
    if c.erg.running_sessions() != 0:
        return "after the server stop, %d session(s) still count as running" % c.erg.running_sessions()
    return None


def case_tabs(c):
    c.start()
    one, two = c.tab(), c.tab()
    c.tell("exit")
    fail = expect_unsaved_after(c, one)
    if fail:
        return "tab 1: " + fail
    msg = c.ended(two)
    if not isinstance(msg, dict) or msg.get("unsaved") is not True:
        return "tab 2's ended frame does not say unsaved: %r" % (msg,)
    # A tab that attached after the ended frame went out gets it again.
    late = json.loads(c.live._ended_frame())
    if late.get("unsaved") is not True:
        return "a late attach's ended frame does not say unsaved: %r" % late
    return None


def case_half(c):
    c.start()
    tab = c.tab()
    c.tell("half")
    return expect_unsaved_after(c, tab)


def case_resumes(c):
    c.start()
    tab = c.tab()
    c.tell("exit")
    fail = expect_unsaved_after(c, tab)
    if fail:
        return fail
    real = c.erg.spawn_grok
    spawned = []
    c.erg.spawn_grok = lambda *a: spawned.append(a) or real(*a)
    out = {}
    try:
        threads = [threading.Thread(target=lambda n=n: out.update({n: resume(c.erg, c.sid)}))
                   for n in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(WAIT)
    finally:
        c.erg.spawn_grok = real
    for n in range(2):
        code, body = out.get(n, (None, {}))
        if code != 404 or body.get("unsaved") is not True:
            return "/resume %d of 2 answered %s %r" % (n + 1, code, body)
    if spawned:
        return "a /resume of an unsaved session started grok"
    return None


def case_slowscan(c):
    live = c.start()
    tab = c.tab()
    real = c.erg.find_session
    inside, release = threading.Event(), threading.Event()

    def slow(sid):
        if sid == c.sid:
            inside.set()
            release.wait(WAIT)
        return real(sid)

    c.erg.find_session = slow
    try:
        c.tell("exit")
        if not inside.wait(WAIT):
            return "the exit never looked for the summary"
        if not c.erg.LIVE_LOCK.acquire(timeout=1):
            return "a slow scan at exit holds LIVE_LOCK"
        c.erg.LIVE_LOCK.release()
        if not live.lock.acquire(timeout=1):
            return "a slow scan at exit holds the Live's lock"
        live.lock.release()
        c.erg.session_state(c.sid, with_turn=False)
    finally:
        release.set()
        c.erg.find_session = real
    return expect_unsaved_after(c, tab)


def case_starting(c):
    real = c.erg.spawn_grok
    spawning, release = threading.Event(), threading.Event()

    def spawn(*a):
        spawning.set()
        release.wait(WAIT)
        return real(*a)

    c.erg.spawn_grok = spawn
    opener = threading.Thread(target=c.start, daemon=True)
    box = {}
    asker = threading.Thread(target=lambda: box.update(got=chat_unsaved(c.port, c.sid)), daemon=True)
    try:
        opener.start()
        if not spawning.wait(WAIT):
            return "the start never reached its spawn"
        asker.start()
        # /chat must be waiting for the spawn, not answering.
        asker.join(0.5)
        release.set()
        asker.join(WAIT * 2)
        opener.join(WAIT)
    finally:
        release.set()
        c.erg.spawn_grok = real
    c.erg.kill_all()
    if box.get("got") != "connecting":
        return "/chat during the new session's start showed %r, not connecting" % box.get("got")
    return None


CASES = [("exit", case_exit), ("saved", case_saved), ("quit", case_quit), ("stop", case_stop),
         ("tabs", case_tabs), ("half", case_half), ("resumes", case_resumes),
         ("slowscan", case_slowscan), ("starting", case_starting)]


def main():
    erg = load()
    os.makedirs(SESSIONS)
    server = ThreadingHTTPServer(("127.0.0.1", 0), erg.Handler)
    server.token = None
    server.tls = False
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    only = sys.argv[1:] or [name for name, _ in CASES]
    failures = []
    with contextlib.redirect_stdout(io.StringIO()):
        try:
            for name, fn in CASES:
                if name not in only:
                    continue
                c = Case(erg, port)
                try:
                    fail = fn(c)
                except Exception as exc:
                    fail = "%s: %s" % (type(exc).__name__, exc)
                finally:
                    c.close()
                    erg.kill_all()
                if fail:
                    failures.append("%s: %s" % (name, fail))
        finally:
            server.shutdown()
            server.server_close()
            shutil.rmtree(TMP, ignore_errors=True)
    for failure in failures:
        print("FAIL: " + failure)
    if failures:
        return 1
    print("PASS: unsaved new sessions: %s" % ", ".join(only))
    return 0


if __name__ == "__main__":
    sys.exit(main())
