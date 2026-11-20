"""A session whose PTY gets a file descriptor of 1024 or more still streams output.

select() cannot take an fd >= 1024 (ValueError), and a long-running server
with many sockets and sessions can hand a new PTY such a number.  The server
starts under a wrapper that raises the soft fd limit and holds fds 3..1099
open, so every fd the server opens, the PTY included, is above 1023.  Then a
saved session is resumed with the stand-in grok (tests/ab/fakegrok.py) and
its first frame must arrive over /term.

Run: [ERGOSPHERE_TEST_PORT=<spare port>] python3 -B tests/high_fd_pty_test.py
Stdlib only, no network, no real grok: "grok" is fakegrok.py under that name.
"""
import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOLD_UP_TO = 1100
WRAPPER = r"""
import os, resource, sys
soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
want = 4096 if hard == resource.RLIM_INFINITY else min(hard, 4096)
if soft < want:
    resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
while True:
    fd = os.open(os.devnull, os.O_RDONLY)
    os.set_inheritable(fd, True)
    if fd >= %d:
        break
os.execv(sys.executable, [sys.executable] + sys.argv[1:])
""" % HOLD_UP_TO


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def post(base, path, body):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(), headers={
        "Origin": base, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def pty_fds(pid):
    found = []
    for name in os.listdir("/proc/%d/fd" % pid):
        try:
            if os.readlink("/proc/%d/fd/%s" % (pid, name)) == "/dev/ptmx":
                found.append(int(name))
        except OSError:
            pass
    return sorted(found)


def read_frame(sock):
    def exact(n):
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise EOFError("socket closed")
            buf += chunk
        return buf
    b1, b2 = exact(2)
    n = b2 & 0x7F
    if n == 126:
        n = struct.unpack("!H", exact(2))[0]
    elif n == 127:
        n = struct.unpack("!Q", exact(8))[0]
    return b1 & 0x0F, exact(n)


def term_output(port, sid, deadline, log_path):
    """Binary /term output until fakegrok's status line shows, or why it never did."""
    sock = socket.create_connection(("127.0.0.1", port), timeout=1)
    key = base64.b64encode(os.urandom(16)).decode()
    sock.sendall((
        "GET /term?resume=%s&cols=80&rows=24 HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n"
        "Origin: http://127.0.0.1:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
        "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n" % (sid, port, port, key)
    ).encode())
    head = b""
    while b"\r\n\r\n" not in head:
        head += sock.recv(1)
    if b" 101 " not in head.split(b"\r\n", 1)[0]:
        return None, "no websocket upgrade: %r" % head[:80]
    got = b""
    while time.monotonic() < deadline:
        if b"filedescriptor out of range" in Path(log_path).read_bytes():
            return got, "reader thread died: ValueError filedescriptor out of range in select()"
        try:
            op, payload = read_frame(sock)
        except socket.timeout:
            continue
        if op == 2:
            got += payload
            if b"off=" in got:
                return got, None
    return got, "no session output within the deadline"


def main():
    tmp = tempfile.mkdtemp(prefix="ergo-highfd-")
    home = os.path.join(tmp, "home")
    work = os.path.join(tmp, "work")
    binp = os.path.join(tmp, "bin")
    sid = str(uuid.uuid4())
    folder = os.path.join(home, ".grok", "sessions", "highfd", sid)
    os.makedirs(folder)
    os.makedirs(work)
    os.makedirs(binp)
    with open(os.path.join(folder, "summary.json"), "w") as fh:
        json.dump({"info": {"id": sid, "cwd": work}, "generated_title": "high fd",
                   "updated_at": "2026-01-01T00:00:00Z"}, fh)
    port = int(os.environ.get("ERGOSPHERE_TEST_PORT") or free_port())
    config = os.path.join(tmp, "config.json")
    with open(config, "w") as fh:
        json.dump({"bind": "127.0.0.1", "port": port}, fh)
    grok = os.path.join(binp, "grok")
    with open(grok, "w") as fh:
        fh.write('#!/bin/bash\nexec -a grok python3 -B "%s" "$@"\n' % (ROOT / "tests" / "ab" / "fakegrok.py"))
    os.chmod(grok, 0o755)
    log_path = os.path.join(tmp, "server.log")
    env = dict(os.environ, HOME=home, ERGOSPHERE_CONFIG=config, PATH=binp + ":" + os.environ["PATH"])
    log = open(log_path, "wb")
    server = subprocess.Popen(
        [sys.executable, "-B", "-c", WRAPPER, "-B", str(ROOT / "ergosphere.py"),
         "--bind", "127.0.0.1", "--port", str(port)],
        cwd=tmp, env=env, stdout=log, stderr=subprocess.STDOUT)
    failures = []
    try:
        deadline = time.monotonic() + 60
        while b"ERGOSPHERE_READY" not in Path(log_path).read_bytes():
            if server.poll() is not None or time.monotonic() > deadline:
                raise SystemExit("FAIL: server did not start: %s" % Path(log_path).read_text()[-400:])
            time.sleep(0.1)
        base = "http://127.0.0.1:%d" % port
        code, text = post(base, "/resume", {"id": sid})
        if code != 200:
            raise SystemExit("FAIL: /resume returned %s %s" % (code, text[:200]))
        fds = []
        deadline = time.monotonic() + 30
        while not fds and time.monotonic() < deadline:
            fds = pty_fds(server.pid)
            time.sleep(0.05)
        print("pty master fd(s): %s" % fds)
        if not fds or min(fds) < 1024:
            failures.append("setup: the PTY fd is not >= 1024 (%s), the test proves nothing" % fds)
        else:
            got, why = term_output(port, sid, time.monotonic() + 30, log_path)
            print("session output bytes: %d" % len(got or b""))
            if why:
                failures.append(why)
    finally:
        server.terminate()
        try:
            server.wait(10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()
        log.close()
        subprocess.run(["pkill", "-f", sid], check=False)
        shutil.rmtree(tmp, ignore_errors=True)
    for failure in failures:
        print("FAIL: " + failure)
    if failures:
        return 1
    print("PASS: session output arrives with a PTY fd >= 1024")
    return 0


if __name__ == "__main__":
    sys.exit(main())
