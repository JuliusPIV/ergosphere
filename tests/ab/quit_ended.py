"""Quit's ended frame reaches every tab, the quitting tab included (issue #212).

The order is forced, never waited for: the quitting tab's sender thread is
held on the frame before the ended frame until `_term` has detached that tab
(on main, detach then drops the queued ended frame).  The other cases guard
the same exit path: a second tab, a tab that attaches after Quit, a sender
stuck on a socket that never drains, and a server stop.

An in-process server from the given code folder on a real loopback socket;
the "grok" is a sleeping python.  Stdlib only, no network, no real grok.

Run: python3 -B tests/ab/quit_ended.py run <code dir> [port]   (JSON on stdout)
     python3 -B tests/ab/quit_ended.py summary <A.json> <B.json>
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
import time
import urllib.parse
import uuid
from http.server import ThreadingHTTPServer

WAIT = 10
# _term joins the sender for 2 s; anything near WAIT means it hung.
TERM_BOUND = 5
ORDER = ["quitting_tab", "second_tab", "late_attach", "slow_socket", "server_stop"]


def load(code):
    spec = importlib.util.spec_from_file_location("ergosphere_quit_ended", os.path.join(code, "ergosphere.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Tab:
    """A raw /term WebSocket client.  A thread collects the server's text frame types."""

    def __init__(self, port, sid):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=WAIT * 3)
        key = base64.b64encode(os.urandom(16)).decode()
        origin = "http://127.0.0.1:%d" % port
        self.sock.sendall(("GET /term?resume=%s HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nOrigin: %s\r\n"
                           "Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
                           "Sec-WebSocket-Version: 13\r\n\r\n" % (sid, port, origin, key)).encode())
        self.reader = self.sock.makefile("rb")
        status = self.reader.readline()
        if b" 101 " not in status:
            raise RuntimeError("no upgrade: %r" % status)
        while self.reader.readline() not in (b"\r\n", b""):
            pass
        self.types = []
        self.closed = False
        self.cond = threading.Condition()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            while True:
                head = self.reader.read(2)
                if len(head) < 2:
                    break
                n = head[1] & 0x7F
                if n == 126:
                    n = int.from_bytes(self.reader.read(2), "big")
                elif n == 127:
                    n = int.from_bytes(self.reader.read(8), "big")
                payload = self.reader.read(n)
                op = head[0] & 0x0F
                if op == 8:
                    break
                if op == 1:
                    kind = json.loads(payload).get("type")
                    with self.cond:
                        self.types.append(kind)
                        self.cond.notify_all()
        except (OSError, ValueError):
            pass
        finally:
            with self.cond:
                self.closed = True
                self.cond.notify_all()

    def got(self, kind):
        with self.cond:
            self.cond.wait_for(lambda: kind in self.types or self.closed, WAIT)
            return kind in self.types

    def wait_closed(self):
        with self.cond:
            return self.cond.wait_for(lambda: self.closed, WAIT)

    def send(self, obj):
        data = json.dumps(obj).encode()
        mask = os.urandom(4)
        body = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(bytes([0x81, 0x80 | len(data)]) + mask + body)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class Hold:
    """Holds one tab's sender thread on its first frame until `release` is set."""

    def __init__(self, on_detach):
        self.on_detach = on_detach
        self.held, self.release = threading.Event(), threading.Event()
        self.term_done = threading.Event()
        self.handler_thread = None
        self.client = None
        self.term_seconds = None
        self.used = False


def install_hooks(erg):
    holds = {}
    real_send, real_term, real_detach = erg.Handler._send_frame, erg.Handler._term, erg.Live.detach

    def sid_of(handler):
        query = urllib.parse.urlsplit(handler.path).query
        return (urllib.parse.parse_qs(query).get("resume") or [""])[0]

    def term(handler):
        # The next /term of that id takes the hold; later tabs of it run free.
        hold = holds.pop(sid_of(handler), None)
        handler._hold = hold
        if hold:
            hold.handler_thread = threading.get_ident()
        start = time.monotonic()
        try:
            real_term(handler)
        finally:
            if hold:
                hold.term_seconds = time.monotonic() - start
                hold.term_done.set()

    def send_frame(handler, opcode, payload):
        hold = getattr(handler, "_hold", None)
        # Only the sender thread: the handler's own frames (session) go straight out.
        if hold and not hold.used and threading.get_ident() != hold.handler_thread:
            hold.used = True
            hold.held.set()
            hold.release.wait(WAIT * 3)
        return real_send(handler, opcode, payload)

    def detach(live, client):
        real_detach(live, client)
        hold = getattr(client.handler, "_hold", None) if client is not None else None
        if hold:
            hold.client = client
            if hold.on_detach:
                hold.release.set()

    erg.Handler._send_frame, erg.Handler._term, erg.Live.detach = send_frame, term, detach
    return holds


def start_live(erg, cwd):
    sid = str(uuid.uuid4())
    erg.open_live(sid, [sys.executable, "-c", "import time; time.sleep(60)", sid], cwd, 80, 24)
    return sid


def held_tab(holds, port, sid, on_detach):
    hold = Hold(on_detach)
    holds[sid] = hold
    tab = Tab(port, sid)
    if not tab.got("session"):
        raise RuntimeError("no session frame: %s" % tab.types)
    if not hold.held.wait(WAIT):
        raise RuntimeError("the tab's sender never sent a frame")
    return tab, hold


def quit_held(holds, port, sid):
    tab, hold = held_tab(holds, port, sid, True)
    tab.send({"type": "stop"})
    if not tab.wait_closed():
        raise RuntimeError("the socket did not close after Quit")
    return tab, hold


def case_quitting_tab(erg, holds, port, cwd):
    tab, _ = quit_held(holds, port, start_live(erg, cwd))
    tab.close()
    ok = "ended" in tab.types
    return ok, "ended frame received" if ok else "no ended frame, got %s" % tab.types


def case_second_tab(erg, holds, port, cwd):
    sid = start_live(erg, cwd)
    other = Tab(port, sid)
    if not other.got("subagents"):
        raise RuntimeError("second tab never attached: %s" % other.types)
    tab, _ = quit_held(holds, port, sid)
    tab.close()
    got = other.got("ended")
    other.close()
    ok = got and "ended" in tab.types
    return ok, "quitting=%s other=%s" % ("ended" in tab.types, got)


def case_late_attach(erg, holds, port, cwd):
    sid = start_live(erg, cwd)
    tab, _ = quit_held(holds, port, sid)
    tab.close()
    late = Tab(port, sid)
    got = late.got("ended")
    late.close()
    ok = got and "ended" in tab.types
    return ok, "quitting=%s late=%s" % ("ended" in tab.types, got)


def case_slow_socket(erg, holds, port, cwd):
    # The sender is stuck in a write that never finishes: Quit must still
    # return within the join bound, and the sender must exit once woken.
    tab, hold = held_tab(holds, port, start_live(erg, cwd), False)
    tab.send({"type": "stop"})
    done = hold.term_done.wait(WAIT)
    hold.release.set()
    sender = hold.client.sender if hold.client is not None else None
    if sender is not None:
        sender.join(WAIT)
    tab.close()
    gone = sender is not None and not sender.is_alive()
    ok = done and hold.term_seconds is not None and hold.term_seconds < TERM_BOUND and gone
    secs = "%.1fs" % hold.term_seconds if hold.term_seconds is not None else "never"
    return ok, "Quit returned in %s, sender exited=%s" % (secs, gone)


def case_server_stop(erg, holds, port, cwd):
    sid = start_live(erg, cwd)
    tab, hold = held_tab(holds, port, sid, False)
    erg.STOP.set()
    try:
        start = time.monotonic()
        erg.kill_all()
        took = time.monotonic() - start
    finally:
        hold.release.set()
    got = tab.got("ended")
    tab.close()
    hold.term_done.wait(WAIT)
    erg.STOP.clear()
    ok = got and took < TERM_BOUND
    return ok, "stop took %.1fs, ended=%s" % (took, got)


CASES = {
    "quitting_tab": case_quitting_tab,
    "second_tab": case_second_tab,
    "late_attach": case_late_attach,
    "slow_socket": case_slow_socket,
    "server_stop": case_server_stop,
}


def run(code, port=0):
    tmp = tempfile.mkdtemp(prefix="ergo-quit-ended-")
    os.environ["HOME"] = tmp
    checks = {}
    with contextlib.redirect_stdout(io.StringIO()):
        erg = load(code)
        erg.find_session = lambda s: {"id": s, "cwd": tmp, "title": ""}
        holds = install_hooks(erg)
        server = ThreadingHTTPServer(("127.0.0.1", int(port)), erg.Handler)
        server.token = None
        server.tls = False
        threading.Thread(target=server.serve_forever, daemon=True).start()
        bound = server.server_address[1]
        try:
            for name in ORDER:
                try:
                    ok, value = CASES[name](erg, holds, bound, tmp)
                except Exception as exc:
                    ok, value = False, "%s: %s" % (type(exc).__name__, exc)
                checks[name] = {"ok": bool(ok), "value": value}
        finally:
            erg.STOP.set()
            erg.kill_all()
            server.shutdown()
            server.server_close()
            shutil.rmtree(tmp, ignore_errors=True)
    return {"checks": checks}


def summary(apath, bpath):
    load_checks = lambda p: (json.load(open(p)).get("checks") or {}) if os.path.exists(p) and os.path.getsize(p) else {}
    a, b = load_checks(apath), load_checks(bpath)
    width = 40
    lines = ["", "Issue #212: Quit's ended frame reaches the quitting tab (sender held until detach)",
             "%-14s %-*s %-*s %s" % ("check", width, "A", width, "B", "same")]
    bad = []
    for name in ORDER:
        ca = a.get(name) or {"ok": False, "value": "not run"}
        cb = b.get(name) or {"ok": False, "value": "not run"}
        cell = lambda c: ("%s %s" % ("PASS" if c["ok"] else "FAIL", c["value"]))[:width]
        if not cb["ok"]:
            bad.append(name)
        lines.append("%-14s %-*s %-*s %s" % (name, width, cell(ca), width, cell(cb), "yes" if ca == cb else "NO"))
    if bad:
        lines.append("FAIL on B: %s" % ", ".join(bad))
    print("\n".join(lines))
    return 1 if bad else 0


def main(argv):
    if len(argv) in (3, 4) and argv[1] == "run":
        print(json.dumps(run(argv[2], argv[3] if len(argv) == 4 else 0)))
        return 0
    if len(argv) == 4 and argv[1] == "summary":
        return summary(argv[2], argv[3])
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
