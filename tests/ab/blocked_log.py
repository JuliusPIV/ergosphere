"""A/B check for issue #70: a blocked log write must not stall the request that logs.

  python3 -B blocked_log.py run <code-dir> <port> <work-dir>  -> JSON checks on stdout
  python3 -B blocked_log.py summary <A-json> <B-json>          -> table on stdout;
                                                                exit 1 when a check passes on A and fails on B

run starts that side's ergosphere.py with stdout and stderr on a pipe, reads
it up to the ready line, then fills the pipe so every further log write
blocks, as a stalled disk or an unread journal pipe does.  Then it launches
the seeded session (/resume logs "term pid=") and reattaches to it over /term
(logs "term reattach").  Both must answer while the log is blocked; once the
pipe is read again both lines must reach the log.  Last, with the pipe full
again, SIGTERM must stop the server.  grok is the stand-in TUI
(fakegrok.py).  Stdlib only.
"""
import base64
import fcntl
import json
import os
import select
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
WAIT = 5.0
STOP_WAIT = 10.0
F_SETPIPE_SZ = 1031  # Linux fcntl.h; not in the fcntl module before Python 3.10
ORDER = ["log_launch", "log_reattach", "log_health", "log_lines_kept", "log_stop"]


def check(ok, value):
    return {"ok": bool(ok), "value": str(value)[:60]}


def request(base, path, body=None, timeout=WAIT):
    headers = {"Origin": base, "Content-Type": "application/json"}
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")
    except (OSError, ValueError) as exc:
        return None, {"error": type(exc).__name__}


def first_frame(port, sid, timeout=WAIT):
    """The type of the first text frame /term?resume=sid sends, or why there is none."""
    origin = "http://127.0.0.1:%s" % port
    try:
        sock = socket.create_connection(("127.0.0.1", int(port)), timeout=timeout)
    except OSError as exc:
        return "connect: %s" % type(exc).__name__
    try:
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall(("GET /term?resume=%s HTTP/1.1\r\nHost: 127.0.0.1:%s\r\nOrigin: %s\r\n"
                      "Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
                      "Sec-WebSocket-Version: 13\r\n\r\n" % (sid, port, origin, key)).encode())
        reader = sock.makefile("rb")
        if b" 101 " not in reader.readline():
            return "no upgrade"
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
    except socket.timeout:
        return "no frame in %.0fs" % timeout
    except (OSError, ValueError) as exc:
        return type(exc).__name__
    finally:
        sock.close()


def read_until(fd, log, want, deadline):
    """Append what the pipe gives to log until every string in want is in it."""
    end = time.monotonic() + deadline
    while not all(w in log[0] for w in want):
        left = end - time.monotonic()
        if left <= 0:
            return False
        if select.select([fd], [], [], left)[0]:
            chunk = os.read(fd, 65536)
            if not chunk:
                return all(w in log[0] for w in want)
            log[0] += chunk.decode("utf-8", "replace")
    return True


def fill(fd):
    """Write to fd until the pipe is full. fd is non-blocking and the server's end is not."""
    total = 0
    for size in (4096, 1):
        while True:
            try:
                total += os.write(fd, b"\0" * size)
            except BlockingIOError:
                break
    return total


def run(code, port, work):
    home, bin_dir = os.path.join(work, "home"), os.path.join(work, "bin")
    sid = str(uuid.uuid4())
    folder = os.path.join(home, ".grok", "sessions", "ab", sid)
    os.makedirs(folder)
    os.makedirs(bin_dir)
    os.makedirs(os.path.join(work, "work"))
    with open(os.path.join(folder, "summary.json"), "w") as handle:
        json.dump({"info": {"id": sid, "cwd": os.path.join(work, "work")},
                   "generated_title": "AB log session", "updated_at": "2026-01-01T00:00:00Z"}, handle)
    with open(os.path.join(bin_dir, "grok"), "w") as handle:
        handle.write('#!/bin/bash\nexec -a grok python3 -B "%s/fakegrok.py" "$@"\n' % HERE)
    os.chmod(os.path.join(bin_dir, "grok"), 0o755)
    config = os.path.join(work, "config.json")
    with open(config, "w") as handle:
        json.dump({"bind": "127.0.0.1", "port": int(port)}, handle)
    env = dict(os.environ, HOME=home, ERGOSPHERE_CONFIG=config, PATH=bin_dir + ":" + os.environ["PATH"])
    env.pop("NOTIFY_SOCKET", None)
    rfd, wfd = os.pipe()
    fcntl.fcntl(wfd, F_SETPIPE_SZ, 4096)
    # A second open of the pipe is its own file description: O_NONBLOCK here
    # leaves the server's end blocking, as a real stalled log is.
    filler = os.open("/proc/self/fd/%d" % wfd, os.O_WRONLY | os.O_NONBLOCK)
    server = subprocess.Popen(
        [sys.executable, "-B", os.path.join(code, "ergosphere.py"), "--bind", "127.0.0.1", "--port", str(port)],
        cwd=work, env=env, stdout=wfd, stderr=wfd)
    os.close(wfd)
    base = "http://127.0.0.1:%s" % port
    log = [""]
    checks = {}
    reader = None
    try:
        if not read_until(rfd, log, ["ERGOSPHERE_READY"], 60):
            raise RuntimeError("server not ready")
        fill(filler)
        began = time.monotonic()
        code_, body = request(base, "/resume", {"id": sid})
        checks["log_launch"] = check(code_ == 200, "resume %s %s in %.1fs" % (
            code_, body.get("error", ""), time.monotonic() - began))
        began = time.monotonic()
        frame = first_frame(port, sid)
        checks["log_reattach"] = check(frame == "session", "first frame %s in %.1fs" % (frame, time.monotonic() - began))
        code_, body = request(base, "/health")
        checks["log_health"] = check(code_ == 200, "health %s" % code_)
        kept = read_until(rfd, log, ["term pid=", "term reattach"], 30)
        checks["log_lines_kept"] = check(kept, "term pid=%s term reattach=%s" % (
            "term pid=" in log[0], "term reattach" in log[0]))
        # Stop while the log is blocked again (a reattach queued one more line):
        # the server must exit, not hang in a log write.
        fill(filler)
        first_frame(port, sid)
        began = time.monotonic()
        server.terminate()
        try:
            rc = server.wait(STOP_WAIT)
            checks["log_stop"] = check(True, "exit %s in %.1fs" % (rc, time.monotonic() - began))
        except subprocess.TimeoutExpired:
            checks["log_stop"] = check(False, "still running %.0fs after SIGTERM" % STOP_WAIT)
    except (RuntimeError, OSError) as exc:
        for name in ORDER:
            checks.setdefault(name, check(False, "not reached: %s" % exc))
    finally:
        # Keep reading so a server stuck in a log write can stop.
        reader = threading.Thread(target=read_until, args=(rfd, log, ["\x00never\x00"], 30), daemon=True)
        reader.start()
        server.terminate()
        try:
            server.wait(20)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()
        os.close(filler)
        reader.join(5)
        with open(os.path.join(work, "server.log"), "w") as handle:
            handle.write(log[0].replace("\0", ""))
    return {"checks": checks}


def summary(apath, bpath):
    load = lambda p: (json.load(open(p)).get("checks") or {}) if os.path.exists(p) else {}
    a, b = load(apath), load(bpath)
    width = 36
    lines = ["", "Issue #70: log writes blocked (stdout/stderr pipe full, nobody reading)",
             "%-18s %-*s %-*s %s" % ("check", width, "A", width, "B", "same")]
    regress = []
    for name in ORDER:
        ca = a.get(name) or {"ok": False, "value": "not run"}
        cb = b.get(name) or {"ok": False, "value": "not run"}
        cell = lambda c: ("%s %s" % ("PASS" if c["ok"] else "FAIL", c["value"]))[:width]
        if ca["ok"] and not cb["ok"]:
            regress.append(name)
        lines.append("%-18s %-*s %-*s %s" % (name, width, cell(ca), width, cell(cb), "yes" if ca == cb else "NO"))
    if regress:
        lines.append("REGRESSION: passes on A, fails on B: %s" % ", ".join(regress))
    failed = [n for n in ORDER if not (b.get(n) or {}).get("ok")]
    if failed:
        lines.append("FAIL on B: %s" % ", ".join(failed))
    print("\n".join(lines))
    return 1 if regress or failed else 0


def main(argv):
    if len(argv) == 5 and argv[1] == "run":
        print(json.dumps(run(argv[2], argv[3], argv[4])))
        return 0
    if len(argv) == 4 and argv[1] == "summary":
        return summary(argv[2], argv[3])
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
