"""SIGTERM and SIGINT stop the server wherever they land (issue #132).

A Python signal handler runs in the main thread between two bytecodes, so it
can land while that thread holds a lock.  A handler that takes the same lock
waits on itself forever: the server is then one thread waiting on a futex and
ignores SIGTERM, as seen once in an install test.  Each case runs a real
server (its own ergosphere.py, stand-in grok from tests/ab/fakegrok.py),
makes the signal land at one exact point, then waits, with a deadline, for
the process to exit 0:

  limbo    one SIGTERM while the main thread starts a request's thread (it
           holds threading's thread-start lock, an RLock on Python 3.12, so
           the old handler's own thread start did not hang there)
  sigint   the same with SIGINT
  livelock a second SIGTERM while the main thread holds LIVE_LOCK in the stop
  startup  SIGTERM during startup, at READY, before the serve loop runs
  session  SIGTERM with a running grok attached from two tabs: grok is gone
  spawn    SIGTERM while a start is held before its spawn (a hung mount),
           released after the signal: no grok is left
  scan     the same with the start held in its /proc holder scan

A Settings restart pending when the signal lands is tests/restart_stop_test.py.

Run: python3 -B tests/sigterm_lock_test.py [case ...]
Port: ERGOSPHERE_TEST_PORT (tests/run_all.sh sets it), else a free one.
Stdlib only, no network, no real grok.  tests/ab/sigterm_lock.py runs the
same cases against both A/B trees.
"""
import http.client
import importlib.util
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FAKEGROK = os.path.join(HERE, "ab", "fakegrok.py")
CASES = ["limbo", "sigint", "livelock", "startup", "session", "spawn", "scan"]
EXIT_WAIT = 20.0
READY_WAIT = 60.0


# ---- inside the server process ("serve" mode) ----

class SignalWhileHeld:
    """Wraps a lock: the first main-thread acquire while armed signals this
    process with the lock held.  The Python handler then runs at the next
    bytecode check, still inside this acquire."""

    def __init__(self, real, signum, marker, armed=None):
        self.real, self.signum, self.marker = real, signum, marker
        self.when = armed
        self.armed = False
        self.fired = False

    def arm(self):
        self.armed = True

    def acquire(self, *args, **kwargs):
        got = self.real.acquire(*args, **kwargs)
        armed = self.when() if self.when else self.armed
        if got and armed and not self.fired and threading.current_thread() is threading.main_thread():
            self.fired = True
            open(self.marker, "w").close()
            os.kill(os.getpid(), self.signum)
        return got

    def release(self):
        self.real.release()

    def locked(self):
        return self.real.locked()

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_exc):
        self.release()


def gate(marker, fifo, real):
    # Mark that the call was reached, wait until the test opens the fifo, go on.
    def held(*args, **kwargs):
        open(marker, "w").close()
        with open(fifo, "rb"):
            pass
        return real(*args, **kwargs)
    return held


def serve(code, case, work, args):
    spec = importlib.util.spec_from_file_location("ergosphere", os.path.join(code, "ergosphere.py"))
    erg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(erg)
    marker = os.path.join(work, "landed")
    if case in ("limbo", "sigint"):
        signum = signal.SIGINT if case == "sigint" else signal.SIGTERM
        lock = SignalWhileHeld(threading._active_limbo_lock, signum, marker)
        threading._active_limbo_lock = lock
        real_process = erg.ErgosphereServer.process_request

        def process_request(self, request, client_address):
            lock.arm()
            return real_process(self, request, client_address)

        erg.ErgosphereServer.process_request = process_request
    elif case == "livelock":
        # Armed once a stop has begun: the stop's own acquire is where the
        # second SIGTERM lands.
        erg.LIVE_LOCK = SignalWhileHeld(erg.LIVE_LOCK, signal.SIGTERM, marker, armed=erg.STOP.is_set)
    elif case == "startup":
        real_notify = erg.sd_notify

        def notify(state):
            if state == "READY=1":
                open(marker, "w").close()
                os.kill(os.getpid(), signal.SIGTERM)
            return real_notify(state)

        erg.sd_notify = notify
    elif case == "spawn":
        erg.spawn_grok = gate(marker, os.path.join(work, "fifo"), erg.spawn_grok)
    elif case == "scan":
        erg.session_holder = gate(marker, os.path.join(work, "fifo"), erg.session_holder)
    return erg.main(args)


# ---- the test side ----

def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def wait_for(fn, deadline):
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        if fn():
            return True
        time.sleep(0.05)
    return False


def request(base, path, body=None, timeout=30):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data,
                                 headers={"Origin": base, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")
    except (OSError, ValueError, http.client.HTTPException) as exc:
        # A stop can cut a reply short.
        return None, {"error": type(exc).__name__}


def grok_pids(sid):
    # sid is a fresh uuid per case, so only this case's grok carries it.
    pids = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open("/proc/%s/cmdline" % pid, "rb") as handle:
                parts = handle.read().split(b"\0")
        except OSError:
            continue
        if parts and parts[0] == b"grok" and sid.encode() in parts:
            pids.append(int(pid))
    return pids


def stuck_where(pid):
    # The issue's symptom: how many threads, and what the main thread waits in.
    try:
        threads = len(os.listdir("/proc/%d/task" % pid))
        with open("/proc/%d/wchan" % pid) as handle:
            wchan = handle.read().strip() or "?"
    except OSError:
        return "gone"
    return "%d thread(s), main waiting in %s" % (threads, wchan)


def setup(work, sid):
    home, bin_dir = os.path.join(work, "home"), os.path.join(work, "bin")
    cwd = os.path.join(work, "proj")
    folder = os.path.join(home, ".grok", "sessions", "t", sid)
    for d in (folder, bin_dir, cwd):
        os.makedirs(d)
    with open(os.path.join(folder, "summary.json"), "w") as handle:
        json.dump({"info": {"id": sid, "cwd": cwd}, "generated_title": "sigterm test",
                   "updated_at": "2026-01-01T00:00:00Z"}, handle)
    grok = os.path.join(bin_dir, "grok")
    with open(grok, "w") as handle:
        # argv[0] must be "grok": Ergosphere checks /proc/<pid>/cmdline.
        handle.write('#!/bin/bash\nexec -a grok python3 -B "%s" "$@"\n' % FAKEGROK)
    os.chmod(grok, 0o755)
    os.mkfifo(os.path.join(work, "fifo"))
    return home, bin_dir


def run_case(code, port, case):
    """One case against code/ergosphere.py.  Returns (ok, detail)."""
    work = tempfile.mkdtemp(prefix="ergo-sigterm-%s-" % case)
    sid = str(uuid.uuid4())
    home, bin_dir = setup(work, sid)
    config = os.path.join(work, "config.json")
    with open(config, "w") as handle:
        json.dump({"bind": "127.0.0.1", "port": int(port)}, handle)
    env = dict(os.environ, HOME=home, ERGOSPHERE_CONFIG=config, PATH=bin_dir + ":" + os.environ["PATH"])
    for name in ("NOTIFY_SOCKET", "WATCHDOG_USEC", "WATCHDOG_PID", "ERGOSPHERE_TOKEN"):
        env.pop(name, None)
    log_path = os.path.join(work, "server.log")
    log = open(log_path, "w")
    server = subprocess.Popen(
        [sys.executable, "-B", os.path.abspath(__file__), "serve", code, case, work,
         "--bind", "127.0.0.1", "--port", str(port)],
        cwd=work, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    base = "http://127.0.0.1:%s" % port
    marker = os.path.join(work, "landed")
    fifo = os.path.join(work, "fifo")
    writer = None
    try:
        if case == "startup":
            if not wait_for(lambda: os.path.exists(marker), READY_WAIT):
                return False, "the signal never landed at READY"
        else:
            ready = lambda: "ERGOSPHERE_READY" in open(log_path).read() or server.poll() is not None
            if not wait_for(ready, READY_WAIT) or server.poll() is not None:
                return False, "server not ready: " + open(log_path).read()[-300:]
        if case in ("limbo", "sigint"):
            # The request's thread start is where the signal lands.
            threading.Thread(target=request, args=(base, "/health", None, 5), daemon=True).start()
            if not wait_for(lambda: os.path.exists(marker), 10):
                return False, "the signal never landed in a thread start"
        elif case == "livelock":
            server.send_signal(signal.SIGTERM)
            if not wait_for(lambda: os.path.exists(marker) or server.poll() is not None, 10):
                return False, "the second SIGTERM never landed under LIVE_LOCK"
            if not os.path.exists(marker):
                return False, "exited before the second SIGTERM landed (rc %s)" % server.returncode
        elif case == "session":
            for _tab in (1, 2):
                code_, body = request(base, "/resume", {"id": sid})
                if code_ != 200:
                    return False, "resume: %s %s" % (code_, body.get("error"))
            if not wait_for(lambda: len(grok_pids(sid)) == 1, 30):
                return False, "grok did not start (%d running)" % len(grok_pids(sid))
            server.send_signal(signal.SIGTERM)
        elif case in ("spawn", "scan"):
            threading.Thread(target=request, args=(base, "/resume", {"id": sid}, 60), daemon=True).start()
            if not wait_for(lambda: os.path.exists(marker), 30):
                return False, "the start never reached the held call"
            server.send_signal(signal.SIGTERM)
            writer = os.open(fifo, os.O_WRONLY)
        try:
            rc = server.wait(EXIT_WAIT)
        except subprocess.TimeoutExpired:
            return False, "still running %.0fs after the signal: %s" % (EXIT_WAIT, stuck_where(server.pid))
        if rc != 0:
            return False, "exit code %s: %s" % (rc, open(log_path).read()[-300:])
        left = len(grok_pids(sid))
        if left:
            return False, "exit 0 but %d grok(s) left for the session" % left
        return True, "exit 0, no grok left"
    finally:
        if writer is None and os.path.exists(marker) and case in ("spawn", "scan"):
            try:
                writer = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
            except OSError:
                pass
        if server.poll() is None:
            os.killpg(server.pid, signal.SIGKILL)
            server.wait()
        if writer is not None:
            os.close(writer)
        log.close()
        # A failed case can leave its stand-in grok (in its own process group).
        for pid in grok_pids(sid):
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                pass
        shutil.rmtree(work, ignore_errors=True)


def wait_port_free(port):
    def free():
        sock = socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", int(port)))
            return True
        except OSError:
            return False
        finally:
            sock.close()
    return wait_for(free, 10)


def run_all(code, port, cases=CASES):
    results = {}
    for case in cases:
        wait_port_free(port)
        results[case] = run_case(code, port, case)
    return results


def main(argv):
    if len(argv) >= 5 and argv[1] == "serve":
        return serve(argv[2], argv[3], argv[4], argv[5:])
    cases = argv[1:] or CASES
    if [c for c in cases if c not in CASES]:
        print(__doc__, file=sys.stderr)
        return 2
    port = int(os.environ.get("ERGOSPHERE_TEST_PORT") or free_port())
    failed = 0
    for case, (ok, detail) in run_all(ROOT, port, cases).items():
        print("%s: %s: %s" % ("PASS" if ok else "FAIL", case, detail))
        failed += not ok
    if failed:
        print("FAIL: %d of %d cases" % (failed, len(cases)))
        return 1
    print("PASS: SIGTERM/SIGINT stop the server in all %d cases" % len(cases))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
