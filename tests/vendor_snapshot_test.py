"""A running server serves the vendor files it started with, not newer ones an
installer wrote over them while it kept running (issue #64).  A Settings
restart loads the new code and the new vendor files together.

Run: [ERGOSPHERE_TEST_PORT=<spare port>] python3 -B tests/vendor_snapshot_test.py [code-dir]
code-dir (default: this checkout) holds the ergosphere.py and vendor/ to test;
tests/ab/run.sh passes each side's tree.
Stdlib only, no network: a copy of ergosphere.py and vendor/ in a throwaway
folder, a real server on loopback with a stub grok, a throwaway HOME and config.
"""
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent
FILES = ("xterm.js", "xterm.css", "addon-fit.js")


def free_port():
    if os.environ.get("ERGOSPHERE_TEST_PORT"):
        return int(os.environ["ERGOSPHERE_TEST_PORT"])
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
    data = resp.read()
    conn.close()
    return resp.status, data


def health(port, deadline, not_started=None):
    """The /health reply once the server answers (and, if given, has a new start time)."""
    while time.time() < deadline:
        try:
            status, data = request(port, "GET", "/health")
            if status == 200:
                reply = json.loads(data)
                if not_started is None or reply.get("started") != not_started:
                    return reply
        except (OSError, ValueError):
            pass
        time.sleep(0.1)
    return None


def main():
    port = free_port()
    work = Path(tempfile.mkdtemp(prefix="ergo-vendor-snapshot-test."))
    code = work / "code"
    (code / "vendor").mkdir(parents=True)
    shutil.copy2(ROOT / "ergosphere.py", code / "ergosphere.py")
    for name in FILES:
        shutil.copy2(ROOT / "vendor" / name, code / "vendor" / name)
    bindir = work / "bin"
    bindir.mkdir()
    stub = bindir / "grok"
    stub.write_text("#!/bin/sh\necho grok-stub 0\n")
    stub.chmod(0o755)
    config = work / "ergosphere.json"
    config.write_text(json.dumps({"bind": "127.0.0.1", "port": port, "agents": {}}), encoding="utf-8")
    env = dict(os.environ)
    env.update({
        "HOME": str(work),
        "PATH": str(bindir) + os.pathsep + env.get("PATH", "/usr/bin:/bin"),
        "ERGOSPHERE_CONFIG": str(config),
    })
    for name in ("ERGOSPHERE_PORT", "ERGOSPHERE_TOKEN", "NOTIFY_SOCKET", "WATCHDOG_USEC", "WATCHDOG_PID"):
        env.pop(name, None)
    proc = subprocess.Popen(
        [sys.executable, "-B", str(code / "ergosphere.py")],
        cwd=str(work), env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    failures = []
    try:
        first = health(port, time.time() + 15)
        if first is None:
            failures.append("server did not come up on %d" % port)
            return failures
        old = {name: (code / "vendor" / name).read_bytes() for name in FILES}
        # The installer replaces vendor/* and keeps this server (live sessions).
        new = {name: old[name] + b"\n/* newer release */\n" for name in FILES}
        for name in FILES:
            (code / "vendor" / name).write_bytes(new[name])

        for name in FILES:
            status, data = request(port, "GET", "/vendor/" + name)
            if status != 200 or data != old[name]:
                failures.append("running server: /vendor/%s HTTP %s, %d bytes, %s; want the %d bytes it started with"
                                % (name, status, len(data), "the new file" if data == new[name] else "other bytes", len(old[name])))

        status, data = request(port, "POST", "/settings", {"action": "restart"})
        if status != 200:
            failures.append("restart HTTP %s %s" % (status, data[:200]))
            return failures
        second = health(port, time.time() + 15, not_started=first.get("started"))
        if second is None:
            failures.append("restarted server did not come back on %d" % port)
            return failures
        for name in FILES:
            status, data = request(port, "GET", "/vendor/" + name)
            if status != 200 or data != new[name]:
                failures.append("after restart: /vendor/%s HTTP %s, %d bytes; want the new %d bytes"
                                % (name, status, len(data), len(new[name])))
        if second.get("code_sha256") == first.get("code_sha256"):
            failures.append("after restart: /health code_sha256 unchanged, want the new vendor files in it")
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
    print("PASS: running server keeps its vendor files; restart loads the new ones")
