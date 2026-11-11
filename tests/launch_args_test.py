"""Launch arguments: grok's saved defaults, and no wait on the model list.

Run: python3 -B tests/launch_args_test.py
Stdlib only, no network, no grok, no server.  HOME is a temp folder.
"""
import importlib.util
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CONFIG = """[models]
default = "grok-4.7"
default_reasoning_effort = "high"

[ui]
permission_mode = "ask"
"""


def load_module():
    spec = importlib.util.spec_from_file_location("ergosphere", ROOT / "ergosphere.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    erg = load_module()
    failures = []

    def check(cond, msg):
        print(("ok:   " if cond else "FAIL: ") + msg)
        if not cond:
            failures.append(msg)

    with tempfile.TemporaryDirectory() as home:
        os.environ["HOME"] = home
        cfg = Path(home) / ".grok" / "config.toml"

        # No config file: pass the flags as before.
        check(erg.grok_config_defaults() is None, "missing config.toml reads as no defaults")
        check(erg.launch_model_effort("grok-4.7", "high") == ("grok-4.7", "high"),
              "missing config.toml passes -m and effort as picked")

        cfg.parent.mkdir()
        cfg.write_text(CONFIG, encoding="utf-8")
        before = cfg.read_bytes()
        check(erg.grok_config_defaults() == {"model": "grok-4.7", "effort": "high"},
              "config.toml defaults are read")
        check(erg.launch_model_effort("grok-4.7", "high") == ("", ""),
              "model and effort equal to the defaults are not passed")
        check(erg.launch_model_effort("grok-4.6", "high") == ("grok-4.6", ""),
              "only a non-default model is passed")
        check(erg.launch_model_effort("grok-4.7", "low") == ("", "low"),
              "only a non-default effort is passed")
        check(erg.launch_model_effort("", "") == ("", ""), "blank picks pass nothing")
        check(cfg.read_bytes() == before, "config.toml is never written")

        # Unparseable file: pass the flags as before.
        cfg.write_text("[models\ndefault = ", encoding="utf-8")
        check(erg.grok_config_defaults() is None, "unparseable config.toml reads as no defaults")
        check(erg.launch_model_effort("grok-4.7", "high") == ("grok-4.7", "high"),
              "unparseable config.toml passes -m and effort as picked")

    # Empty model cache: a launch must not wait on `grok models` nor reject a model.
    calls = []
    started = threading.Event()

    def slow_models():
        calls.append(1)
        started.set()
        time.sleep(3)
        return [], "grok models timed out"

    erg.run_grok_models = slow_models
    erg._MODELS_CACHE.update({"at": 0.0, "value": None, "refreshing": False})
    # A /models request is already fetching the first list (holds the lock).
    fetch = threading.Thread(target=erg.grok_models_list, daemon=True)
    fetch.start()
    started.wait(2)
    t0 = time.monotonic()
    try:
        got = erg.checked_model_effort("grok-4.7", "high")
    except ValueError as exc:
        got = "ValueError: %s" % exc
    took = time.monotonic() - t0
    check(got == ("grok-4.7", "high"), "empty cache: a named model is accepted (got %r)" % (got,))
    check(took < 1.0, "empty cache: launch check does not wait on grok models (%.2fs)" % took)
    fetch.join(5)

    erg._MODELS_CACHE.update({"at": 0.0, "value": None, "refreshing": False})
    try:
        erg.checked_model_effort("bad model!", "")
        check(False, "empty cache: a malformed model is still rejected")
    except ValueError:
        check(True, "empty cache: a malformed model is still rejected")

    # A good list, when there is one, still rejects a model it does not name.
    erg._MODELS_CACHE.update({"at": time.monotonic(), "value": ([{"id": "grok-4.7", "efforts": []}], ""),
                              "refreshing": False})
    try:
        erg.checked_model_effort("grok-9", "")
        check(False, "with a list: an unlisted model is rejected")
    except ValueError:
        check(True, "with a list: an unlisted model is rejected")

    text = (ROOT / "ergosphere.py").read_text(encoding="utf-8")
    check("grok saves a model or effort you choose as its new default" in text,
          "new-session dialog notes that grok saves the choice as its default")

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    check("default_reasoning_effort" in readme, "README FAQ explains grok saving the model and effort")

    if failures:
        print("%d failed" % len(failures))
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
