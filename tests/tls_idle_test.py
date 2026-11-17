"""With TLS on, a TCP client that connects and never sends a ClientHello must
not stop other clients from being served, and the server drops it after the
handshake timeout.

Run: [ERGOSPHERE_TEST_PORT=<spare port>] python3 -B tests/tls_idle_test.py
Stdlib plus the openssl CLI (throwaway self-signed cert), no network: a real
server on a loopback port, a stub grok, a throwaway HOME and config.
"""
import json
import os
import shutil
import socket
import ssl
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


def https_health(port, timeout):
    ctx = ssl._create_unverified_context()
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as raw:
        with ctx.wrap_socket(raw, server_hostname="127.0.0.1") as tls:
            tls.sendall(b"GET /health HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nConnection: close\r\n\r\n" % port)
            data = b""
            while True:
                chunk = tls.recv(65536)
                if not chunk:
                    break
                data += chunk
    head, _sep, body = data.partition(b"\r\n\r\n")
    return head.split(b"\r\n", 1)[0].decode("ascii", "replace"), json.loads(body or b"{}")


def wait_up(port, deadline):
    while time.time() < deadline:
        try:
            return https_health(port, 1)
        except (OSError, ValueError):
            time.sleep(0.1)
    return None


def main():
    work = tempfile.mkdtemp(prefix="ergo-tls-test.")
    bindir = os.path.join(work, "bin")
    os.mkdir(bindir)
    stub = os.path.join(bindir, "grok")
    with open(stub, "w") as f:
        f.write("#!/bin/sh\necho grok-stub 0\n")
    os.chmod(stub, 0o755)
    cert, key = os.path.join(work, "cert.pem"), os.path.join(work, "key.pem")
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
         "-subj", "/CN=localhost", "-keyout", key, "-out", cert],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    port = free_port()
    env = dict(os.environ)
    env.update({
        "HOME": work,
        "PATH": bindir + os.pathsep + env.get("PATH", "/usr/bin:/bin"),
        "ERGOSPHERE_CONFIG": os.path.join(work, "ergosphere.json"),
    })
    for name in ("ERGOSPHERE_TOKEN", "NOTIFY_SOCKET", "WATCHDOG_USEC", "WATCHDOG_PID"):
        env.pop(name, None)
    proc = subprocess.Popen(
        [sys.executable, "-B", str(ROOT / "ergosphere.py"), "--bind", "127.0.0.1", "--port", str(port),
         "--tls-cert", cert, "--tls-key", key],
        cwd=work, env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    failures = []
    idle = None
    try:
        if wait_up(port, time.time() + 15) is None:
            failures.append("server did not answer https /health within 15s")
        else:
            idle = socket.create_connection(("127.0.0.1", port), timeout=30)
            time.sleep(0.3)
            start = time.time()
            try:
                status, body = https_health(port, 3)
                took = time.time() - start
                if status.split(" ")[1:2] != ["200"] or body.get("tls") is not True:
                    failures.append("https /health with an idle client open: %s %r" % (status, body))
                else:
                    print("ok: https /health answered in %.2fs while an idle TCP client was open" % took)
            except (OSError, ValueError) as exc:
                failures.append("https /health got no answer while an idle TCP client was open (%s after %.1fs)"
                                % (exc.__class__.__name__, time.time() - start))
            # The idle client is dropped once the handshake timeout passes.
            idle.settimeout(20)
            start = time.time()
            try:
                dropped = idle.recv(1) == b""
            except ConnectionResetError:
                dropped = True
            except socket.timeout:
                dropped = False
            if not dropped:
                failures.append("idle client still open after %.0fs" % (time.time() - start))
            else:
                print("ok: idle client dropped after %.1fs" % (time.time() - start))
    finally:
        if idle is not None:
            idle.close()
        proc.terminate()
        try:
            out = proc.communicate(timeout=10)[0]
        except subprocess.TimeoutExpired:
            proc.kill()
            out = proc.communicate()[0]
        shutil.rmtree(work, ignore_errors=True)
    for failure in failures:
        print("FAIL: " + failure)
    if failures:
        print("server output: %s" % out.decode("utf-8", "replace")[-800:])
        return 1
    print("PASS: an idle TCP client does not block TLS clients, and is dropped after the handshake timeout")
    return 0


if __name__ == "__main__":
    sys.exit(main())
