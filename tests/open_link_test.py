"""A link grok opens reaches the page as an "open" frame, never this machine's display (issue #34).

grok opens a clicked link with the Rust webbrowser crate: it runs $BROWSER with
the URL, and with no BROWSER and no DISPLAY it shows "Can't open links without
a display".  A fake grok here does what grok does on a click: it splits
$BROWSER on whitespace, puts the URL in for %s and runs it.  It also reports
whether it got the server's DISPLAY.  Checks:
  display: grok's env has no DISPLAY / WAYLAND_DISPLAY (they name the server's desktop);
  browser: grok's env has a BROWSER;
  open: the attached page gets {"type": "open", "url": <the https link>};
  refused: javascript:, file: and user:pass@ links never reach the page;
  token: /open-link with a wrong token is refused (404);
  rate/scheme/ended: with the grok's own token, a second open inside the
    pause is 429, a javascript: link 400, and after the grok is gone 404;
  self: links to this server (loopback names, short IPv4, ::1, a "\\@" trick)
    are 400: a tab there would be same-origin with the page;
  inherited: with no helper, grok gets no ERGOSPHERE_LINK_* from the server's env.

Run: python3 -B tests/open_link_test.py
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
import urllib.error
import urllib.request
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = tempfile.mkdtemp(prefix="ergo-open-link-")
os.environ["HOME"] = TMP
# The server's own desktop and browser: grok must not see either.
os.environ["DISPLAY"] = ":77"
os.environ["WAYLAND_DISPLAY"] = "wayland-77"
os.environ["BROWSER"] = "/bin/false"
WAIT = 15
GOOD = "https://example.com/x?a=1"
BAD = ["javascript:alert(1)", "file:///etc/passwd", "https://user:pw@example.com/"]
SELF_URLS = ["http://127.0.0.1:%d/models", "http://localhost:%d/", "http://127.1:%d/",
             "http://[::1]:%d/", "http://127.0.0.1:%d\\@example.com/"]

FAKE = r'''
import os, subprocess, sys
env = "display=%s wayland=%s browser=%s" % (
    "set" if os.environ.get("DISPLAY") else "unset",
    "set" if os.environ.get("WAYLAND_DISPLAY") else "unset",
    "set" if os.environ.get("BROWSER") else "unset")
sys.stdout.write("ENV " + env + "\r\n")
sys.stdout.flush()
os.read(0, 16)  # the test's click (a line: the pty is cooked): the page is attached now
for url in sys.argv[1:]:
    browser = os.environ.get("BROWSER", "")
    args = [a.replace("%s", url) for a in browser.split()]
    if not args:
        sys.stdout.write("NOBROWSER\r\n")
        continue
    if "%s" not in browser:
        args.append(url)
    rc = subprocess.call(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    sys.stdout.write("RC %d\r\n" % rc)
sys.stdout.write("DONE\r\n")
sys.stdout.flush()
import time; time.sleep(60)
'''


def load():
    spec = importlib.util.spec_from_file_location("ergosphere", ROOT / "ergosphere.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def ws_open(port, sid):
    sock = socket.create_connection(("127.0.0.1", port), timeout=WAIT)
    key = base64.b64encode(os.urandom(16)).decode()
    sock.sendall(("GET /term?resume=%s HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nOrigin: http://127.0.0.1:%d\r\n"
                  "Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
                  "Sec-WebSocket-Version: 13\r\n\r\n" % (sid, port, port, key)).encode())
    reader = sock.makefile("rb")
    if b" 101 " not in reader.readline():
        raise RuntimeError("no websocket upgrade")
    while reader.readline() not in (b"\r\n", b""):
        pass
    return sock, reader


def ws_send(sock, data):
    mask = os.urandom(4)
    body = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    sock.sendall(bytes([0x82, 0x80 | len(data)]) + mask + body)


def ws_frames(reader):
    while True:
        head = reader.read(2)
        if len(head) < 2:
            return
        n = head[1] & 0x7F
        if n == 126:
            n = int.from_bytes(reader.read(2), "big")
        elif n == 127:
            n = int.from_bytes(reader.read(8), "big")
        yield head[0] & 0x0F, reader.read(n)


def inherited_env(erg):
    # A server started inside another grok, with no helper of its own: its
    # grok must not get the outer grok's link variables.
    saved = erg.LINK_URL
    erg.LINK_URL = None
    os.environ["ERGOSPHERE_LINK_URL"] = "http://127.0.0.1:1/open-link"
    os.environ["ERGOSPHERE_LINK_TOKEN"] = "outer-grok-token"
    code = ("import os; print(os.environ.get('ERGOSPHERE_LINK_URL', 'unset'),"
            " os.environ.get('ERGOSPHERE_LINK_TOKEN', 'unset'), 'END')")
    try:
        proc, master = erg.spawn_grok([sys.executable, "-c", code], TMP, 80, 24)
    finally:
        erg.LINK_URL = saved
        os.environ.pop("ERGOSPHERE_LINK_URL", None)
        os.environ.pop("ERGOSPHERE_LINK_TOKEN", None)
    out = b""
    try:
        while b"END" not in out:
            data = os.read(master, 4096)
            if not data:
                break
            out += data
    except OSError:
        pass  # EIO once the child has exited and the pty is drained
    proc.wait(WAIT)
    os.close(master)
    return out.decode("utf-8", "replace").split("END")[0].strip()


def post(port, obj):
    req = urllib.request.Request("http://127.0.0.1:%d/open-link" % port, json.dumps(obj).encode(),
                                 {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=WAIT) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def main():
    erg = load()
    server = ThreadingHTTPServer(("127.0.0.1", 0), erg.Handler)
    server.token = None
    server.tls = False
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    erg.LINK_URL = "http://127.0.0.1:%d/open-link" % port
    sid = str(uuid.uuid4())
    failures = []
    opens, text = [], b""
    with contextlib.redirect_stdout(io.StringIO()):
        try:
            erg.open_live(sid, [sys.executable, "-c", FAKE] + BAD + [GOOD], TMP, 80, 24)
            sock, reader = ws_open(port, sid)
            sock.settimeout(WAIT)
            ws_send(sock, b"x\r")
            # The fake writes DONE after its last helper exited, and the server
            # queues an open frame before it answers that helper.
            try:
                for opcode, payload in ws_frames(reader):
                    if opcode == 1:
                        msg = json.loads(payload)
                        if msg.get("type") == "open":
                            opens.append(msg.get("url"))
                    else:
                        text += payload
                    if b"DONE" in text:
                        break
            except OSError as exc:
                failures.append("no DONE from the fake grok within %ds (%s)" % (WAIT, exc))
            sock.close()
            wrong = post(port, {"token": "not-the-token", "url": GOOD})
            token = getattr(erg.LIVE[sid].proc, "link_token", None) if sid in erg.LIVE else None
            # A pause no machine can outrun: the helper's open was the last.
            erg.LINK_PAUSE = 3600
            again = post(port, {"token": token, "url": GOOD})
            script = post(port, {"token": token, "url": "javascript:alert(1)"})
            # This server itself: a tab there is same-origin with the page (#220 review).
            selfs = [post(port, {"token": token, "url": u % port}) for u in SELF_URLS]
            erg.kill_all()
            # grok gone (quit, exited, server restarting): its token opens nothing.
            ended = post(port, {"token": token, "url": GOOD})
            inherited = inherited_env(erg)
        finally:
            erg.kill_all()
            server.shutdown()
            server.server_close()
            shutil.rmtree(TMP, ignore_errors=True)
    env = text.split(b"ENV ", 1)[1].split(b"\r\n", 1)[0].decode() if b"ENV " in text else "?"
    if "display=unset" not in env or "wayland=unset" not in env:
        failures.append("display: grok got the server's display (%s)" % env)
    if "browser=set" not in env:
        failures.append("browser: grok got no BROWSER, so a click shows \"Can't open links without a display\" (%s)" % env)
    if opens != [GOOD]:
        failures.append("open/refused: page got open frames %r, want only %r" % (opens, [GOOD]))
    if wrong != 404:
        failures.append("token: a wrong token got %r, want 404" % wrong)
    if not token:
        failures.append("token: the live grok has no link token")
    elif (again, script, ended) != (429, 400, 404):
        failures.append("rate/scheme/ended: got %r, want (429, 400, 404)" % ((again, script, ended),))
    if token and selfs != [400] * len(SELF_URLS):
        failures.append("self: links to this server got %r, want 400 each" % list(zip(SELF_URLS, selfs)))
    if inherited != "unset unset":
        failures.append("inherited: a grok with no helper got the parent grok's link vars (%s)" % inherited)
    for failure in failures:
        print("FAIL: " + failure)
    if failures:
        return 1
    print("PASS: grok's link reaches the page as an open frame; display stripped; bad links and tokens refused")
    return 0


if __name__ == "__main__":
    sys.exit(main())
