"""A SIGTERM or SIGINT that lands while a Settings restart is pending makes
`serve` exit 0 instead of re-exec'ing the server (issue #46).

The signal lands in serve_forever, or after a restart already ended
serve_forever: inside the final kill_all or server_close (#132 review).

Run: python3 -B tests/restart_stop_test.py
Stdlib only, no network: `serve` runs in this process with a fake listener.
"""
import argparse
import importlib.util
import os
import signal
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WHERE = ("serve_forever", "kill_all", "server_close")


class FakeListener:
    server_address = ("127.0.0.1", 1)
    socket = None

    def __init__(self, signum, where):
        self.signum, self.where = signum, where

    def serve_forever(self):
        if self.where == "serve_forever":
            os.kill(os.getpid(), self.signum)
            time.sleep(0.2)

    def shutdown(self):
        pass

    def server_close(self):
        if self.where == "server_close":
            os.kill(os.getpid(), self.signum)


def check(signum, where):
    spec = importlib.util.spec_from_file_location("ergosphere", ROOT / "ergosphere.py")
    erg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(erg)
    for name in ("NOTIFY_SOCKET", "WATCHDOG_USEC", "ERGOSPHERE_TOKEN", erg.RESTART_FALLBACK_ENV):
        os.environ.pop(name, None)
    execs = []
    erg.listen = lambda *_a: FakeListener(signum, where)
    erg.restart_self = lambda *a: execs.append(a) or 0
    erg.print = lambda *a, **k: None
    if where == "kill_all":
        real_kill_all = erg.kill_all
        fired = []

        def kill_all():
            # Once: an older handler calls kill_all itself.
            if not fired:
                fired.append(1)
                os.kill(os.getpid(), signum)
            real_kill_all()

        erg.kill_all = kill_all
    erg.RESTART.set()
    args = argparse.Namespace(bind="127.0.0.1", port=1, tls_cert=None, tls_key=None)
    saved = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        rc = erg.serve(args, {"agents": {}})
    finally:
        for s, handler in saved.items():
            signal.signal(s, handler)
    name = "%s in %s" % (signal.Signals(signum).name, where)
    if execs:
        return ["%s during a pending restart re-exec'd the server (restart_self%r)" % (name, execs[0])]
    if rc != 0:
        return ["%s during a pending restart: serve returned %r, want 0" % (name, rc)]
    return []


def main():
    failures = []
    for where in WHERE:
        failures += check(signal.SIGTERM, where) + check(signal.SIGINT, where)
    for failure in failures:
        print("FAIL: " + failure)
    if failures:
        return 1
    print("PASS: SIGTERM and SIGINT during a pending restart exit 0 without re-exec (%s)" % ", ".join(WHERE))
    return 0


if __name__ == "__main__":
    sys.exit(main())
