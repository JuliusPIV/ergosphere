"""A Settings restart whose 200 reply cannot be written still shuts the
server down for the re-exec, so `RESTART` never stays set while it keeps
serving and refusing every launch (issue #47).

Run: python3 -B tests/restart_reply_test.py
Stdlib only, no network: the handler runs against a fake server, and the
reply write raises as it does when the client reset the socket.
"""
import argparse
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
        self.config = {"bind": "127.0.0.1", "port": 1}
        self.stopped = threading.Event()

    def shutdown(self):
        self.stopped.set()


def check(exc_type):
    spec = importlib.util.spec_from_file_location("ergosphere", ROOT / "ergosphere.py")
    erg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(erg)
    erg.print = lambda *a, **k: None
    handler = erg.Handler.__new__(erg.Handler)
    handler.server = FakeServer()
    handler._bound_endpoint = lambda: ("127.0.0.1", 1)

    def reply(code, obj):
        raise exc_type("client gone")

    handler._json = reply
    try:
        handler._settings_restart()
    except exc_type:
        pass
    name = exc_type.__name__
    if not erg.RESTART.is_set():
        return ["%s on the reply: RESTART was cleared, want the restart to go ahead" % name]
    if not handler.server.stopped.wait(2):
        return ["%s on the reply: RESTART set but server.shutdown never ran, so every launch is refused" % name]
    return []


def main():
    # The restart reads the config file; a missing one keeps the fake
    # server's settings, whatever config the developer has.
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["ERGOSPHERE_CONFIG"] = os.path.join(tmp, "missing.json")
        failures = check(BrokenPipeError) + check(ConnectionResetError)
    for failure in failures:
        print("FAIL: " + failure)
    if failures:
        return 1
    print("PASS: a restart whose reply fails still shuts the server down for the re-exec")
    return 0


if __name__ == "__main__":
    sys.exit(main())
