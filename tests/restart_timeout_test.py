"""A Settings restart whose checks run past the installer's wait must not
restart the server later (#63 review r2-1). The installer's curl gives up
after 10 s and then reports the server as left running; a restart that
resumes after that would still shut the server down for the re-exec.

Run: python3 -B tests/restart_timeout_test.py
Stdlib only, no network: the handler runs against a fake server, and the
config read is slow, as on a hung mount.
"""
import argparse
import importlib.util
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class FakeServer:
    def __init__(self):
        self.args = argparse.Namespace(cli_bind=None, cli_port=None, env_port=None, bind="127.0.0.1", port=1)
        self.config = {"bind": "127.0.0.1", "port": 1}
        self.stopped = threading.Event()

    def shutdown(self):
        self.stopped.set()


def run_slow_restart(erg, delay):
    """Run one restart whose config read takes `delay` seconds. Returns (reply code, shut down)."""
    handler = erg.Handler.__new__(erg.Handler)
    handler.server = FakeServer()
    handler._bound_endpoint = lambda: ("127.0.0.1", 1)
    replies = []
    handler._json = lambda code, obj: replies.append(code)
    saved = erg.Handler._saved_config

    def slow_saved_config(self):
        time.sleep(delay)
        return saved(self)

    handler._saved_config = slow_saved_config.__get__(handler)
    handler._settings_restart()
    shut = handler.server.stopped.wait(2)
    return (replies[0] if replies else None), shut


def main():
    spec = importlib.util.spec_from_file_location("ergosphere", ROOT / "ergosphere.py")
    erg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(erg)
    erg.print = lambda *a, **k: None
    # Scale the installer's wait down so the test runs fast: the reply must
    # come back before the client gives up, so a slow check gets no restart.
    erg.RESTART_BUDGET = 0.3
    failures = []
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["ERGOSPHERE_CONFIG"] = os.path.join(tmp, "missing.json")
        code, shut = run_slow_restart(erg, 1.0)
        erg.RESTART.clear()
        if shut:
            failures.append("a restart whose checks took 1.0 s (past the 0.3 s budget) still shut the server down")
        if code == 200:
            failures.append("a restart past the budget replied 200 after the client may have given up")
        if erg.RESTART.is_set():
            failures.append("a restart past the budget left RESTART set, so launches stay refused")
        code, shut = run_slow_restart(erg, 0)
        erg.RESTART.clear()
        if code != 200 or not shut:
            failures.append("a fast restart no longer restarts (reply %s, shut down %s)" % (code, shut))
    for failure in failures:
        print("FAIL: " + failure)
    if failures:
        return 1
    print("PASS: a restart past the installer's wait does not restart the server")
    return 0


if __name__ == "__main__":
    sys.exit(main())
