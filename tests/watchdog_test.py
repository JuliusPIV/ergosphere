"""Under systemd Type=notify the server says READY=1 and pings WATCHDOG=1,
and stops pinging while it cannot serve (LIVE_LOCK held).

Run: [ERGOSPHERE_TEST_PORT=<spare port>] python3 -B tests/watchdog_test.py
Stdlib only, no network: a real server on a free loopback port, with a stub
grok, a throwaway HOME and config, and our own abstract NOTIFY_SOCKET.
"""
import importlib.util
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def free_port():
    if os.environ.get("ERGOSPHERE_TEST_PORT"):
        return int(os.environ["ERGOSPHERE_TEST_PORT"])
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def recv_until(sock, want, deadline):
    got = []
    while time.time() < deadline:
        sock.settimeout(max(0.05, deadline - time.time()))
        try:
            msg = sock.recv(4096).decode("ascii", "replace")
        except socket.timeout:
            break
        got.append(msg)
        if want in msg.split("\n"):
            return got, True
    return got, False


def check_live_server():
    work = tempfile.mkdtemp(prefix="ergo-wd-test.")
    bindir = os.path.join(work, "bin")
    os.mkdir(bindir)
    stub = os.path.join(bindir, "grok")
    with open(stub, "w") as f:
        f.write("#!/bin/sh\necho grok-stub 0\n")
    os.chmod(stub, 0o755)
    name = "ergo-wd-test-%d" % os.getpid()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.bind("\0" + name)
    port = free_port()
    env = dict(os.environ)
    env.update({
        "HOME": work,
        "PATH": bindir + os.pathsep + env.get("PATH", "/usr/bin:/bin"),
        "ERGOSPHERE_CONFIG": os.path.join(work, "ergosphere.json"),
        "NOTIFY_SOCKET": "@" + name,
        "WATCHDOG_USEC": "1000000",
    })
    env.pop("WATCHDOG_PID", None)
    env.pop("ERGOSPHERE_TOKEN", None)
    proc = subprocess.Popen(
        [sys.executable, "-B", str(ROOT / "ergosphere.py"), "--bind", "127.0.0.1", "--port", str(port)],
        cwd=work, env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    failures = []
    try:
        got, ready = recv_until(sock, "READY=1", time.time() + 15)
        if not ready:
            failures.append("no READY=1 within 15s (got %r)" % got)
        else:
            got, pinged = recv_until(sock, "WATCHDOG=1", time.time() + 5)
            if not pinged:
                failures.append("no WATCHDOG=1 within 5s of READY (got %r)" % got)
    finally:
        proc.terminate()
        try:
            out = proc.communicate(timeout=10)[0]
        except subprocess.TimeoutExpired:
            proc.kill()
            out = proc.communicate()[0]
        sock.close()
        shutil.rmtree(work, ignore_errors=True)
    if failures:
        failures.append("server output: %s" % out.decode("utf-8", "replace")[-800:])
    return failures


def check_lock_held_blocks_ping():
    spec = importlib.util.spec_from_file_location("ergosphere", ROOT / "ergosphere.py")
    erg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(erg)
    if not hasattr(erg, "can_serve"):
        return ["no self-check (can_serve) before a watchdog ping"]
    erg.inspect_health = lambda *a, **k: "match"
    httpd = types.SimpleNamespace(server_address=("127.0.0.1", 1), tls=False, token=None)
    failures = []
    if not erg.can_serve(httpd, timeout=0.2):
        failures.append("can_serve is False on a healthy server")
    erg.LIVE_LOCK.acquire()
    try:
        if erg.can_serve(httpd, timeout=0.2):
            failures.append("can_serve is True while LIVE_LOCK is held (a deadlock would keep pinging)")
    finally:
        erg.LIVE_LOCK.release()
    return failures


def main():
    failures = check_live_server() + check_lock_held_blocks_ping()
    for failure in failures:
        print("FAIL: " + failure)
    if failures:
        return 1
    print("PASS: READY=1 and WATCHDOG=1 sent; no ping while LIVE_LOCK is held")
    return 0


if __name__ == "__main__":
    sys.exit(main())
