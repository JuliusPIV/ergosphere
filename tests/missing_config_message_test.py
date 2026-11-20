"""The startup message names the ERGOSPHERE_CONFIG path when that file is
missing, not the default name ergosphere.json (issue #123).

Run: [ERGOSPHERE_TEST_PORT=<spare port>] python3 -B tests/missing_config_message_test.py
Stdlib only, no network: a real server on loopback with a stub grok and a
throwaway HOME; the config file is never created.
"""
import http.client
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def free_port():
    if os.environ.get("ERGOSPHERE_TEST_PORT"):
        return int(os.environ["ERGOSPHERE_TEST_PORT"])
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_health(port, deadline):
    while time.time() < deadline:
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            conn.request("GET", "/health")
            status = conn.getresponse().status
            conn.close()
            if status == 200:
                return True
        except OSError:
            pass
        time.sleep(0.1)
    return False


def main():
    port = free_port()
    work = tempfile.mkdtemp(prefix="ergo-missing-config-test.")
    bindir = os.path.join(work, "bin")
    os.mkdir(bindir)
    stub = os.path.join(bindir, "grok")
    with open(stub, "w") as f:
        f.write("#!/bin/sh\necho grok-stub 0\n")
    os.chmod(stub, 0o755)
    missing = Path(work) / "missing.json"
    env = dict(os.environ)
    env.update({
        "HOME": work,
        "PATH": bindir + os.pathsep + env.get("PATH", "/usr/bin:/bin"),
        "ERGOSPHERE_CONFIG": str(missing),
    })
    for name in ("ERGOSPHERE_PORT", "ERGOSPHERE_TOKEN", "NOTIFY_SOCKET", "WATCHDOG_USEC", "WATCHDOG_PID"):
        env.pop(name, None)
    proc = subprocess.Popen(
        [sys.executable, "-B", str(ROOT / "ergosphere.py"), "--bind", "127.0.0.1", "--port", str(port)],
        cwd=work, env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    failures = []
    try:
        if not wait_health(port, time.time() + 15):
            failures.append("server did not come up on %d" % port)
    finally:
        proc.terminate()
        try:
            out = proc.communicate(timeout=10)[0]
        except subprocess.TimeoutExpired:
            proc.kill()
            out = proc.communicate()[0]
        shutil.rmtree(work, ignore_errors=True)
    text = out.decode("utf-8", "replace")
    lines = [line for line in text.splitlines() if line.startswith("config: no ")]
    want = "config: no %s, using defaults and no agents" % missing
    if want not in lines:
        failures.append("want line %r, got %r" % (want, lines))
    if failures:
        failures.append("server output: %s" % text[-800:])
    return failures


if __name__ == "__main__":
    failures = main()
    for line in failures:
        print("FAIL: " + line)
    if failures:
        sys.exit(1)
    print("PASS: startup message names the missing ERGOSPHERE_CONFIG path")
