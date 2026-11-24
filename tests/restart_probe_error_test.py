"""A Settings restart that fails before its reply (the port probe raises, as
EMFILE does in `socket()`) clears `RESTART`, so the server that keeps serving
does not refuse every launch (issue #104).

Run: python3 -B tests/restart_probe_error_test.py
Stdlib only, no network: the handler runs against a fake server whose saved
port differs from the running one, so the probe runs and raises.
"""
import argparse
import errno
import importlib.util
import os
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class FakeServer:
    def __init__(self):
        self.args = argparse.Namespace(cli_bind=None, cli_port=None, env_port=None, bind="127.0.0.1", port=1)
        self.config = {"bind": "127.0.0.1", "port": 2}
        self.stopped = threading.Event()

    def shutdown(self):
        self.stopped.set()


def main():
    spec = importlib.util.spec_from_file_location("ergosphere", ROOT / "ergosphere.py")
    erg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(erg)
    erg.print = lambda *a, **k: None

    def probe(bind, port):
        raise OSError(errno.EMFILE, "Too many open files")

    erg.endpoint_problem = probe
    handler = erg.Handler.__new__(erg.Handler)
    handler.server = FakeServer()
    handler._bound_endpoint = lambda: ("127.0.0.1", 1)
    handler._json = lambda code, obj: None
    raised = None
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["ERGOSPHERE_CONFIG"] = os.path.join(tmp, "missing.json")
        try:
            handler._settings_restart()
        except OSError as exc:
            raised = exc
    failures = []
    if raised is None:
        failures.append("the probe error did not reach the handler; the test no longer exercises it")
    if erg.RESTART.is_set() and not handler.server.stopped.is_set():
        failures.append("probe raised %r: RESTART still set and no shutdown, so every launch is refused" % raised)
    for failure in failures:
        print("FAIL: " + failure)
    if failures:
        return 1
    print("PASS: a restart whose port probe raises clears RESTART")
    return 0


if __name__ == "__main__":
    sys.exit(main())
