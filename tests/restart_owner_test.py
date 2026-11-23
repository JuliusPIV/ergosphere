"""Only the Settings restart request that set `RESTART` may clear it (PR #172,
issue #104).  A second restart request that overlaps one already under way
answers 409 and leaves the flag alone, so the first still restarts and no
launch is accepted in between.

Each case forces one ordering with events, no sleeps:
  second_in_probe   first request held inside its port probe; a second request
                    (probe raises, or reports a problem) runs; the first goes on
  second_after_stop first request done, shutdown started; a second request runs
  owner_fails       the owner's probe raises or reports a problem: cleared, and
                    a later restart can claim the flag
  sigterm_in_probe  SIGTERM while the owner is in its probe; then the probe
                    raises or passes: `serve` exits 0, never re-execs
  launch_in_probe   a launch while the owner is in its probe is refused; once
                    the owner fails, a launch gets past the restart gate
  busy              a grok spawning: restart refused, flag never set

Run: python3 -B tests/restart_owner_test.py [case]
Stdlib only, no network: the handler runs against a fake server, and the
port probe is replaced per request thread.
"""
import argparse
import errno
import importlib.util
import os
import signal
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WAIT = 10  # upper bound for one forced step, not a timing assumption


class FakeServer:
    def __init__(self):
        self.args = argparse.Namespace(cli_bind=None, cli_port=None, env_port=None, bind="127.0.0.1", port=1)
        # Saved port differs from the running one, so the probe runs.
        self.config = {"bind": "127.0.0.1", "port": 2}
        self.stopped = threading.Event()

    def shutdown(self):
        self.stopped.set()


class Past(Exception):
    """open_live got past its RESTART check (raised by the stub holder scan)."""


class World:
    """One fresh ergosphere module and one probe behaviour per request thread."""

    def __init__(self):
        spec = importlib.util.spec_from_file_location("ergosphere", ROOT / "ergosphere.py")
        self.erg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.erg)
        self.erg.print = lambda *a, **k: None
        self.server = FakeServer()
        self.probe = {}  # thread name -> "ok" | "raise" | "problem" | Gate
        self.erg.endpoint_problem = self._probe

        def holder(_sid):
            raise Past()
        self.erg.session_holder = holder

    def _probe(self, _bind, _port):
        how = self.probe.get(threading.current_thread().name, "ok")
        if isinstance(how, Gate):
            how = how.hold()
        if how == "raise":
            raise OSError(errno.EMFILE, "Too many open files")
        if how == "problem":
            return "port 2 is in use"
        return ""

    def request(self, name, how):
        """Run one restart request in a thread; returns (thread, result dict)."""
        self.probe[name] = how
        result = {"replies": [], "raised": None}
        handler = self.erg.Handler.__new__(self.erg.Handler)
        handler.server = self.server
        handler._bound_endpoint = lambda: ("127.0.0.1", 1)
        handler._json = lambda code, obj: result["replies"].append((code, obj))

        def run():
            try:
                handler._settings_restart()
            except BaseException as exc:  # the probe error is re-raised by design
                result["raised"] = exc
        t = threading.Thread(target=run, name=name, daemon=True)
        t.start()
        return t, result

    def launch_gate(self):
        """'refused' when open_live stops at RESTART, 'past' when it gets through."""
        try:
            self.erg.open_live("00000000-0000-4000-8000-000000000000", ["grok"], "/", 80, 24)
        except Past:
            return "past"
        except RuntimeError as exc:
            if "restarting" in str(exc):
                return "refused"
            return "other: %s" % exc
        return "other: returned"


class Gate:
    """Holds a probe until released, then behaves as `then`."""

    def __init__(self):
        self.entered = threading.Event()
        self.go = threading.Event()
        self.then = "ok"

    def hold(self):
        self.entered.set()
        if not self.go.wait(WAIT):
            raise RuntimeError("gate never released")
        return self.then

    def release(self, then):
        self.then = then
        self.go.set()


def finish(t):
    t.join(WAIT)
    return not t.is_alive()


def first_code(result):
    return result["replies"][0][0] if result["replies"] else None


def case_second_in_probe():
    failures = []
    for second in ("raise", "problem"):
        w = World()
        gate = Gate()
        ta, ra = w.request("A", gate)
        if not gate.entered.wait(WAIT):
            return ["second_in_probe: first request never reached its probe"]
        tb, rb = w.request("B", second)
        if not finish(tb):
            return ["second_in_probe: second request hung"]
        if w.launch_gate() != "refused":
            failures.append("second_in_probe/%s: a launch was let in while the first restart was still pending" % second)
        gate.release("ok")
        if not finish(ta) or not w.server.stopped.wait(WAIT):
            failures.append("second_in_probe/%s: the first restart did not start shutdown" % second)
        if first_code(ra) != 200:
            failures.append("second_in_probe/%s: first restart replied %r, want 200" % (second, ra["replies"]))
        if first_code(rb) != 409:
            failures.append("second_in_probe/%s: second request replied %r (raised %r), want 409"
                            % (second, rb["replies"], rb["raised"]))
        if not w.erg.RESTART.is_set():
            failures.append("second_in_probe/%s: second request cleared RESTART after the first went ahead,"
                            " so the server exits instead of restarting" % second)
    return failures


def case_second_after_stop():
    failures = []
    for second in ("raise", "problem"):
        w = World()
        ta, ra = w.request("A", "ok")
        if not finish(ta) or not w.server.stopped.wait(WAIT):
            return ["second_after_stop: first restart did not start shutdown"]
        tb, rb = w.request("B", second)
        if not finish(tb):
            return ["second_after_stop: second request hung"]
        if first_code(rb) != 409:
            failures.append("second_after_stop/%s: second request replied %r (raised %r), want 409"
                            % (second, rb["replies"], rb["raised"]))
        if not w.erg.RESTART.is_set():
            failures.append("second_after_stop/%s: second request cleared RESTART during shutdown,"
                            " so the server exits instead of restarting" % second)
        if w.launch_gate() != "refused":
            failures.append("second_after_stop/%s: a launch was let in during the restart's shutdown" % second)
    return failures


def case_owner_fails():
    failures = []
    for how in ("raise", "problem"):
        w = World()
        ta, ra = w.request("A", how)
        if not finish(ta):
            return ["owner_fails: request hung"]
        if w.erg.RESTART.is_set() or w.server.stopped.is_set():
            failures.append("owner_fails/%s: RESTART still set (or shutdown started) after the owner failed" % how)
        if how == "problem" and first_code(ra) != 409:
            failures.append("owner_fails/problem: replied %r, want 409" % ra["replies"])
        if how == "raise" and not isinstance(ra["raised"], OSError):
            failures.append("owner_fails/raise: the probe error did not reach the handler (%r)" % ra["raised"])
        tb, rb = w.request("B", "ok")
        if not finish(tb) or first_code(rb) != 200 or not w.erg.RESTART.is_set() or not w.server.stopped.wait(WAIT):
            failures.append("owner_fails/%s: a later restart could not go ahead (%r)" % (how, rb["replies"]))
    return failures


class FakeListener:
    server_address = ("127.0.0.1", 1)
    socket = None

    def __init__(self, world, then):
        self.world = world
        self.then = then
        self.ok = True

    def serve_forever(self):
        w = self.world
        gate = Gate()
        ta, _ra = w.request("A", gate)
        if not gate.entered.wait(WAIT):
            self.ok = False
            return
        os.kill(os.getpid(), signal.SIGTERM)
        # The handler runs at the next bytecode boundary of this (main) thread.
        while not (w.erg.STOP.is_set() or getattr(w.erg, "STOP_SIGNALED", False)):
            pass
        gate.release(self.then)
        self.ok = finish(ta)

    def shutdown(self):
        pass

    def server_close(self):
        pass


def case_sigterm_in_probe():
    failures = []
    for then in ("raise", "ok"):
        w = World()
        erg = w.erg
        for name in ("NOTIFY_SOCKET", "WATCHDOG_USEC", "ERGOSPHERE_TOKEN", erg.RESTART_FALLBACK_ENV):
            os.environ.pop(name, None)
        execs = []
        listener = FakeListener(w, then)
        erg.listen = lambda *_a: listener
        erg.restart_self = lambda *a: execs.append(a) or 0
        args = argparse.Namespace(bind="127.0.0.1", port=1, tls_cert=None, tls_key=None)
        saved = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
        try:
            rc = erg.serve(args, {"agents": {}})
        finally:
            for s, handler in saved.items():
                signal.signal(s, handler)
        if not listener.ok:
            failures.append("sigterm_in_probe/%s: the forced ordering did not happen" % then)
        if execs or rc != 0:
            failures.append("sigterm_in_probe/%s: SIGTERM during a restart gave rc=%r exec=%r, want 0 and no exec"
                            % (then, rc, execs))
    return failures


def case_launch_in_probe():
    w = World()
    gate = Gate()
    ta, _ra = w.request("A", gate)
    if not gate.entered.wait(WAIT):
        return ["launch_in_probe: request never reached its probe"]
    failures = []
    if w.launch_gate() != "refused":
        failures.append("launch_in_probe: a launch was let in while a restart was pending")
    gate.release("raise")
    if not finish(ta):
        return ["launch_in_probe: request hung"]
    got = w.launch_gate()
    if got != "past":
        failures.append("launch_in_probe: after the restart failed a launch is %s, want let past the gate" % got)
    return failures


def case_busy():
    w = World()
    w.erg.STARTING.add("x")
    ta, ra = w.request("A", "ok")
    if not finish(ta):
        return ["busy: request hung"]
    if first_code(ra) != 409 or w.erg.RESTART.is_set() or w.server.stopped.is_set():
        return ["busy: a restart with a grok spawning replied %r, RESTART=%s" % (ra["replies"], w.erg.RESTART.is_set())]
    return []


CASES = {
    "second_in_probe": case_second_in_probe,
    "second_after_stop": case_second_after_stop,
    "owner_fails": case_owner_fails,
    "sigterm_in_probe": case_sigterm_in_probe,
    "launch_in_probe": case_launch_in_probe,
    "busy": case_busy,
}


def main(argv):
    names = argv[1:] or list(CASES)
    failures = []
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["ERGOSPHERE_CONFIG"] = os.path.join(tmp, "missing.json")
        for name in names:
            got = CASES[name]()
            failures += got
            print("%s %s" % ("PASS" if not got else "FAIL", name))
    for failure in failures:
        print("FAIL: " + failure)
    if failures:
        return 1
    print("PASS: only the restart request that set RESTART clears it")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
