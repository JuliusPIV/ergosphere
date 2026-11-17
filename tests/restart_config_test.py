"""A Settings restart reports, and the Settings page shows, the port in the
config file, not the server's in-memory copy, when the file was changed
outside the server while it runs (issue #48).

Run: [ERGOSPHERE_TEST_PORT=<spare port>] [ERGOSPHERE_TEST_PORT2=<spare port>] python3 -B tests/restart_config_test.py
Stdlib only, no network: a real server on loopback with a stub grok, a
throwaway HOME and config.  The second port is a free one from the OS
unless ERGOSPHERE_TEST_PORT2 is set.
"""
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def free_port(var):
    if os.environ.get(var):
        return int(os.environ[var])
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def request(port, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Origin": "http://127.0.0.1:%d" % port}
    if body is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(body)
    conn.request(method, path, body=body, headers=headers)
    resp = conn.getresponse()
    data = resp.read().decode("utf-8", "replace")
    conn.close()
    return resp.status, data


def wait_health(port, deadline):
    while time.time() < deadline:
        try:
            status, _ = request(port, "GET", "/health")
            if status == 200:
                return True
        except OSError:
            pass
        time.sleep(0.1)
    return False


def main():
    old_port = free_port("ERGOSPHERE_TEST_PORT")
    new_port = free_port("ERGOSPHERE_TEST_PORT2")
    while new_port == old_port:
        new_port = free_port("")
    work = tempfile.mkdtemp(prefix="ergo-restart-config-test.")
    bindir = os.path.join(work, "bin")
    os.mkdir(bindir)
    stub = os.path.join(bindir, "grok")
    with open(stub, "w") as f:
        f.write("#!/bin/sh\necho grok-stub 0\n")
    os.chmod(stub, 0o755)
    config = Path(work) / "ergosphere.json"
    config.write_text(json.dumps({"bind": "127.0.0.1", "port": old_port, "agents": {}}), encoding="utf-8")
    env = dict(os.environ)
    env.update({
        "HOME": work,
        "PATH": bindir + os.pathsep + env.get("PATH", "/usr/bin:/bin"),
        "ERGOSPHERE_CONFIG": str(config),
    })
    for name in ("ERGOSPHERE_PORT", "ERGOSPHERE_TOKEN", "NOTIFY_SOCKET", "WATCHDOG_USEC", "WATCHDOG_PID"):
        env.pop(name, None)
    proc = subprocess.Popen(
        [sys.executable, "-B", str(ROOT / "ergosphere.py")],
        cwd=work, env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    failures = []
    try:
        if not wait_health(old_port, time.time() + 15):
            failures.append("server did not come up on %d" % old_port)
            return failures
        # The installer or a hand edit changes the port while the server runs.
        config.write_text(json.dumps({"bind": "127.0.0.1", "port": new_port, "agents": {}}), encoding="utf-8")

        _, page = request(old_port, "GET", "/settings")
        match = re.search(r"var state = (\{.*?\});\n", page)
        shown = json.loads(match.group(1)).get("port") if match else None
        if shown != new_port:
            failures.append("Settings page shows saved port %r, want %d (the file)" % (shown, new_port))

        status, data = request(old_port, "POST", "/settings", {"action": "restart"})
        reply = json.loads(data) if status == 200 else {}
        if reply.get("port") != new_port:
            failures.append("restart reply HTTP %s %s, want port %d (the file)" % (status, data[:200], new_port))
        if not wait_health(new_port, time.time() + 15):
            failures.append("restarted server not listening on %d" % new_port)
    finally:
        proc.terminate()
        try:
            out = proc.communicate(timeout=10)[0]
        except subprocess.TimeoutExpired:
            proc.kill()
            out = proc.communicate()[0]
        shutil.rmtree(work, ignore_errors=True)
        if failures:
            failures.append("server output: %s" % out.decode("utf-8", "replace")[-800:])
    return failures


if __name__ == "__main__":
    failures = main()
    for line in failures:
        print("FAIL: " + line)
    if failures:
        sys.exit(1)
    print("PASS: Settings page and restart reply use the port in the config file")
