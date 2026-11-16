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
import threading
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
        # Pings come every 5 s (a quarter of WATCHDOG_USEC): one within 2 s of READY=1 is the immediate one.
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
    the next ping past systemd's deadline (WATCHDOG_USEC after the last good ping)."""
    spec = importlib.util.spec_from_file_location("ergosphere", ROOT / "ergosphere.py")
    erg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(erg)
    os.environ["NOTIFY_SOCKET"] = "@ergo-wd-test-unused"
    os.environ["WATCHDOG_USEC"] = "2000000"  # deadline 2 s
    os.environ.pop("WATCHDOG_PID", None)
    pings, calls = [], [0]

    def fake_can_serve(_httpd, **_kw):
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


def check_short_watchdog_lock_hold():
    """WatchdogSec=4 (deadline 4 s, check every 1 s) and LIVE_LOCK busy for 2.5 s
    once: the check's own waits must fit, so a ping still lands within 4 s of
    the last. The hold ends at 6.1 s, just after the check that starts at 6.0 s,
    so the expected gap is about 3.1 s."""
    spec = importlib.util.spec_from_file_location("ergosphere", ROOT / "ergosphere.py")
    erg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(erg)
    os.environ["NOTIFY_SOCKET"] = "@ergo-wd-test-unused"
    os.environ["WATCHDOG_USEC"] = "4000000"
    os.environ.pop("WATCHDOG_PID", None)
    pings = []
    erg.inspect_health = lambda *a, **k: "match"
    erg.sd_notify = lambda msg: pings.append(time.monotonic()) or True
    erg.print = lambda *a, **k: None
    httpd = types.SimpleNamespace(server_address=("127.0.0.1", 1), tls=False, token=None)
    thread = erg.start_watchdog(httpd)
    time.sleep(3.6)

    def hold():
        with erg.LIVE_LOCK:
            time.sleep(2.5)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    time.sleep(3.5)
    erg.STOP.set()
    thread.join(5)
    gaps = [b - a for a, b in zip(pings, pings[1:])]
    if not gaps or max(gaps) >= 4.0:
        return ["WatchdogSec=4 with a 2.5 s LIVE_LOCK hold: longest gap between pings %.2fs (deadline 4 s)"
                % (max(gaps) if gaps else 0)]
    return []


def check_short_watchdog_transient_stall():
    """WatchdogSec=2 (deadline 2 s, check every 0.5 s) and /health hung for 1 s
    once: the failed checks are retried soon enough that a ping still lands
    within 2 s of the last good one (no restart). Expected gap about 1.25 s."""
    spec = importlib.util.spec_from_file_location("ergosphere", ROOT / "ergosphere.py")
    erg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(erg)
    os.environ["NOTIFY_SOCKET"] = "@ergo-wd-test-unused"
    os.environ["WATCHDOG_USEC"] = "2000000"
    os.environ.pop("WATCHDOG_PID", None)
    stall_from = time.monotonic() + 0.6
    stall_until = stall_from + 1.0
    pings = []

    def inspect(_host, _port, _tls, _token=None, timeout=1):
        # A hung server: a request that starts during the stall blocks for the whole timeout.
        if stall_from <= time.monotonic() < stall_until:
            time.sleep(timeout)
            return "reject"
        return "match"

    erg.inspect_health = inspect
    erg.sd_notify = lambda msg: pings.append(time.monotonic()) or True
    erg.print = lambda *a, **k: None
    httpd = types.SimpleNamespace(server_address=("127.0.0.1", 1), tls=False, token=None)
    thread = erg.start_watchdog(httpd)
    time.sleep(2.5)
    erg.STOP.set()
    thread.join(5)
    before = [p for p in pings if p < stall_from]
    after = [p for p in pings if p >= stall_from]
    if not before or not after:
        return ["no ping on both sides of the 1 s stall (%d pings)" % len(pings)]
    gap = after[0] - before[-1]
    if gap >= 2.0:
        return ["WatchdogSec=2 with a 1 s /health stall: next ping %.2fs after the last good one (deadline 2 s)" % gap]

    # With a very short WatchdogSec the check's own waits must still be at most
    # a quarter interval each. STOP is set, so the loop runs exactly one check.
    os.environ["WATCHDOG_USEC"] = "500000"
    waits = []
    erg.can_serve = lambda _httpd, timeout, health_timeout: waits.append((timeout, health_timeout)) or True
    erg.start_watchdog(httpd).join(5)
    quarter = 0.5 / 4 / 4
    if not waits or max(waits[0]) > quarter:
        return ["WatchdogSec=0.5: self-check waits %r exceed a quarter interval (%.3fs)" % (waits, quarter)]
    return []


def check_sessions_do_not_inherit_notify_env():
    """The notify variables are for the server only: a grok session that
    inherited them could ping the watchdog or report READY=1 as the server."""
    spec = importlib.util.spec_from_file_location("ergosphere", ROOT / "ergosphere.py")
    erg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(erg)
    names = ("NOTIFY_SOCKET", "WATCHDOG_USEC", "WATCHDOG_PID")
    os.environ.update({"NOTIFY_SOCKET": "@ergo-wd-test-unused", "WATCHDOG_USEC": "2000000",
                       "WATCHDOG_PID": str(os.getpid())})
    try:
        proc, master = erg.spawn_grok([shutil.which("env")], "/", 80, 24)
    finally:
        for name in names:
            os.environ.pop(name, None)
    out = b""
    try:
        while True:
            chunk = os.read(master, 65536)
            if not chunk:
                break
            out += chunk
    except OSError:
        pass  # EIO: the child closed its side of the pty
    os.close(master)
    proc.wait(5)
    text = out.decode("utf-8", "replace")
    if "TERM=xterm-256color" not in text:
        return ["could not read the session environment (got %r)" % text[-200:]]
    leaked = [name for name in names if name + "=" in text]
    if leaked:
        return ["a spawned session inherits %s from the server" % ", ".join(leaked)]
    return []


def main():
    failures = (check_live_server() + check_lock_held_blocks_ping() + check_one_slow_miss_keeps_deadline()
                + check_short_watchdog_lock_hold() + check_short_watchdog_transient_stall()
                + check_sessions_do_not_inherit_notify_env())
    for failure in failures:
        print("FAIL: " + failure)
    if failures:
        return 1
    print("PASS: READY=1 and WATCHDOG=1 sent; no ping while LIVE_LOCK is held; a slow miss is retried in time")
    return 0


if __name__ == "__main__":
    sys.exit(main())
