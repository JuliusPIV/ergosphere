"""An expired model list is served at once and refreshed in the background.

Run: python3 -B tests/models_cache_test.py
Stdlib only, no network, no grok, no server: subprocess.run is stubbed.
"""
import importlib.util
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SLOW = 1.5  # seconds each stubbed `grok models` run takes


def load_module():
    spec = importlib.util.spec_from_file_location("ergosphere", ROOT / "ergosphere.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Proc:
    def __init__(self, out, code=0):
        self.stdout = out.encode()
        self.stderr = b"" if code == 0 else b"boom"
        self.returncode = code


def main():
    erg = load_module()
    failures = []
    calls = []
    script = {"out": "  - grok-a (default)\n", "code": 0}

    def fake_run(args, **kw):
        calls.append(kw.get("cwd"))
        time.sleep(SLOW)
        return Proc(script["out"], script["code"])

    erg.grok_bin = lambda: "/bin/true"
    erg.subprocess.run = fake_run

    # 1. First call with no list waits for the one run.
    models, err = erg.grok_models_list()
    if err or [m["id"] for m in models] != ["grok-a"]:
        failures.append("first call: %r %r" % (models, err))
    home = os.path.expanduser("~")
    if calls[-1] != home:
        failures.append("grok models cwd: expected %s, got %r" % (home, calls[-1]))

    # 2. Expired cache: answer at once, with the old list, while a refresh runs.
    erg._MODELS_CACHE["at"] -= erg.MODELS_TTL + 1
    script["out"] = "  - grok-b (default)\n"
    before = len(calls)
    t0 = time.monotonic()
    models, err = erg.grok_models_list()
    elapsed = time.monotonic() - t0
    if elapsed > 0.3:
        failures.append("expired cache blocked for %.2fs" % elapsed)
    if [m["id"] for m in models] != ["grok-a"]:
        failures.append("expired cache did not serve the old list: %r" % models)

    # 3. A launch check during the refresh is not blocked either.
    t0 = time.monotonic()
    try:
        erg.checked_model_effort("grok-a", "")
    except ValueError as exc:
        failures.append("launch check failed during refresh: %s" % exc)
    if time.monotonic() - t0 > 0.3:
        failures.append("launch check blocked during refresh")

    # 4. One refresh at a time: many stale requests during it start no more.
    threads = [threading.Thread(target=erg.grok_models_list) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    time.sleep(SLOW + 0.5)
    if len(calls) - before != 1:
        failures.append("expected 1 background refresh, got %d" % (len(calls) - before))
    models, err = erg.grok_models_list()
    if [m["id"] for m in models] != ["grok-b"]:
        failures.append("background refresh did not land: %r" % models)

    # 5. A failed refresh keeps the old list.
    erg._MODELS_CACHE["at"] -= erg.MODELS_TTL + 1
    script["code"] = 1
    erg.grok_models_list()
    time.sleep(SLOW + 0.5)
    models, err = erg.grok_models_list()
    if err or [m["id"] for m in models] != ["grok-b"]:
        failures.append("failed refresh lost the old list: %r %r" % (models, err))

    # 6. A refresh thread that cannot start: old list served, flag cleared,
    # and a later stale request starts a refresh.
    script["code"] = 0
    script["out"] = "  - grok-c (default)\n"
    # Step 5's last call may have started another (failing) refresh: let it end.
    for _ in range(50):
        if not erg._MODELS_CACHE["refreshing"]:
            break
        time.sleep(0.1)
    erg._MODELS_CACHE["value"] = ([{"id": "grok-b", "efforts": []}], "")
    erg._MODELS_CACHE["at"] -= erg.MODELS_TTL + 1
    real_start = threading.Thread.start

    def failing_start(self):
        threading.Thread.start = real_start
        raise RuntimeError("can't start new thread")

    threading.Thread.start = failing_start
    try:
        models, err = erg.grok_models_list()
    except RuntimeError as exc:
        failures.append("thread start failure leaked to the request: %s" % exc)
        models, err = [], "raised"
    finally:
        threading.Thread.start = real_start
    if err or [m["id"] for m in models] != ["grok-b"]:
        failures.append("thread start failure did not serve the old list: %r %r" % (models, err))
    if erg._MODELS_CACHE["refreshing"]:
        failures.append("thread start failure left the refreshing flag set")
    before = len(calls)
    erg.grok_models_list()
    time.sleep(SLOW + 0.5)
    models, err = erg.grok_models_list()
    if len(calls) - before != 1 or [m["id"] for m in models] != ["grok-c"]:
        failures.append("no refresh after a failed thread start: %d runs, %r" % (len(calls) - before, models))

    for line in failures:
        print("FAIL " + line)
    if failures:
        return 1
    print("PASS expired model list served at once and refreshed in the background")
    return 0


if __name__ == "__main__":
    sys.exit(main())
