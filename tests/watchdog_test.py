"""Under systemd Type=notify the server says READY=1 and pings WATCHDOG=1 at
once, pings again across a Settings restart, and stops pinging while it
cannot serve (LIVE_LOCK held).

Run: [ERGOSPHERE_TEST_PORT=<spare port>] python3 -B tests/watchdog_test.py
Stdlib only, no network: a real server on a free loopback port, with a stub
grok, a throwaway HOME and config, and our own abstract NOTIFY_SOCKET.
"""
import http.client
import importlib.util
import json
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
        "WATCHDOG_USEC": "20000000",
    })
    env.pop("WATCHDOG_PID", None)
    env.pop("ERGOSPHERE_TOKEN", None)
    proc = subprocess.Popen(
        [sys.executable, "-B", str(ROOT / "ergosphere.py"), "--bind", "127.0.0.1", "--port", str(port)],
        cwd=work, env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    failures = []
    try:
        # The interval is 10 s: a ping within 2 s of READY=1 is the immediate one.
        got, ready = recv_until(sock, "READY=1", time.time() + 15)
        if not ready:
            failures.append("no READY=1 within 15s (got %r)" % got)
        else:
            got, pinged = recv_until(sock, "WATCHDOG=1", time.time() + 2)
            if not pinged:
                failures.append("no WATCHDOG=1 within 2s of READY (got %r)" % got)
            else:
                # Settings restart (exec, same pid): a ping before exec, then
                # READY=1 and an immediate ping from the new process.
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request("POST", "/settings", body=json.dumps({"action": "restart"}),
                             headers={"Content-Type": "application/json",
                                      "Origin": "http://127.0.0.1:%d" % port})
                status = conn.getresponse().status
                conn.close()
                end = time.time() + 8
                for want in ("WATCHDOG=1", "READY=1", "WATCHDOG=1"):
                    got, seen = recv_until(sock, want, end)
                    if not seen:
                        failures.append("restart (HTTP %s): no %s in order within 8s (got %r)" % (status, want, got))
                        break
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


def check_one_slow_miss_keeps_deadline():
    """One self-check that fails slowly (like a LIVE_LOCK timeout) must not push
    the next ping past systemd's deadline (2 intervals after the last good ping)."""
    spec = importlib.util.spec_from_file_location("ergosphere", ROOT / "ergosphere.py")
    erg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(erg)
    os.environ["NOTIFY_SOCKET"] = "@ergo-wd-test-unused"
    os.environ["WATCHDOG_USEC"] = "2000000"  # deadline 2 s, ping every 1 s
    os.environ.pop("WATCHDOG_PID", None)
    pings, calls = [], [0]

    def fake_can_serve(_httpd, timeout=2):
        calls[0] += 1
        if calls[0] == 2:
            time.sleep(0.4)
            return False
        return True

    erg.can_serve = fake_can_serve
    erg.sd_notify = lambda msg: pings.append(time.monotonic()) or True
    erg.print = lambda *a, **k: None
    thread = erg.start_watchdog(object())
    time.sleep(4.5)
    erg.STOP.set()
    thread.join(3)
    gaps = [b - a for a, b in zip(pings, pings[1:])]
    if len(pings) < 3:
        return ["only %d pings in 4.5 s" % len(pings)]
    if max(gaps) >= 2.0:
        return ["after one slow miss the next ping came %.2fs after the last good one (deadline 2 s)" % max(gaps)]
    return []


def main():
    failures = check_live_server() + check_lock_held_blocks_ping() + check_one_slow_miss_keeps_deadline()
    for failure in failures:
        print("FAIL: " + failure)
    if failures:
        return 1
    print("PASS: READY=1 and WATCHDOG=1 sent; no ping while LIVE_LOCK is held; a slow miss is retried in time")
    return 0


if __name__ == "__main__":
    sys.exit(main())
