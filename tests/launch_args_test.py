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
        if erg.tomllib is None:
            # Python < 3.11: saved defaults cannot be read; flags as picked.
            print("note: no tomllib on this Python; saved-default checks skipped")
            check(erg.grok_config_defaults() is None, "no tomllib here: config.toml is not read")
            check(erg.launch_model_effort("grok-4.7", "high") == ("grok-4.7", "high"),
                  "no tomllib here: -m and effort are passed as picked")
        else:
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
            page = erg.dashboard_page([], [], {})
            check('var savedDefaults = {"model": "grok-4.7", "effort": "high"};' in page
                  or "var savedDefaults = {\"model\":\"grok-4.7\",\"effort\":\"high\"};" in page,
                  "list page carries the saved defaults before /models answers")

            # Only fields the user changed ("touched") may pass a flag.
            # S1: grok's default changed outside the page (now low); the page
            # still shows high; an untouched Create passes nothing.
            cfg.write_text(CONFIG.replace('"high"', '"low"'), encoding="utf-8")
            check(erg.launch_model_effort("grok-4.7", "high", set()) == ("", ""),
                  "S1: untouched stale effort passes no flag")
            # J1: after a new-tab launch grok saved low; a reopened, untouched
            # dialog still sends the old pick: nothing passed.
            check(erg.launch_model_effort("grok-4.7", "medium", set()) == ("", ""),
                  "J1: untouched values after a new-tab launch pass no flag")
            # Grok (a): saved model not listed, last-used model shown instead.
            check(erg.launch_model_effort("grok-4.6", "medium", set()) == ("", ""),
                  "untouched preselected model and effort pass no flags")
            # An explicit pick still passes its flag (and only that one).
            check(erg.launch_model_effort("grok-4.7", "high", {"effort"}) == ("", "high"),
                  "touched effort different from the saved one is passed")
            check(erg.launch_model_effort("grok-4.6", "low", {"model"}) == ("grok-4.6", ""),
                  "touched model is passed, untouched effort is not")
            check(erg.launch_model_effort("grok-4.7", "low", {"model", "effort"}) == ("", ""),
                  "touched values equal to the current saved defaults pass nothing")
            check(erg.launch_touched({}) == {"model", "effort"} and
                  erg.launch_touched({"touched": ["effort", "x"]}) == {"effort"},
                  "missing touched counts both as chosen; unknown names are ignored")
            try:
                erg.launch_touched({"touched": "effort"})
                check(False, "a malformed touched field is rejected")
            except ValueError:
                check(True, "a malformed touched field is rejected")

        # Unparseable file: pass the flags as before.
        cfg.write_text("[models\ndefault = ", encoding="utf-8")
        check(erg.grok_config_defaults() is None, "unparseable config.toml reads as no defaults")
        check(erg.launch_model_effort("grok-4.7", "high") == ("grok-4.7", "high"),
              "unparseable config.toml passes -m and effort as picked")

    # Python < 3.11 (no tomllib): saved defaults are unknown, so the flags
    # are passed as picked (the behaviour before saved-default protection).
    saved = sys.modules.get("tomllib")
    sys.modules["tomllib"] = None  # makes `import tomllib` raise ImportError
    try:
        old = load_module()
    finally:
        if saved is None:
            sys.modules.pop("tomllib", None)
        else:
            sys.modules["tomllib"] = saved
    check(old.tomllib is None, "no tomllib: module loads with tomllib = None")
    with tempfile.TemporaryDirectory() as home:
        os.environ["HOME"] = home
        cfg = Path(home) / ".grok" / "config.toml"
        cfg.parent.mkdir()
        cfg.write_text(CONFIG, encoding="utf-8")
        check(old.grok_config_defaults() is None, "no tomllib: config.toml is not read")
        check(old.launch_model_effort("grok-4.7", "high") == ("grok-4.7", "high"),
              "no tomllib: -m and effort are passed as picked")

    # The list page substitutes its placeholders in one pass: an agent or
    # folder named like a placeholder must reach the page script intact.
    with tempfile.TemporaryDirectory() as home:
        os.environ["HOME"] = home
        page = erg.dashboard_page([], ["@@SAVED@@", "@@ROWS@@"], {"@@CWDS@@": "/srv/@@AGENTS@@"})
        check('"@@SAVED@@"' in page and '"@@ROWS@@"' in page and '"/srv/@@AGENTS@@"' in page,
              "placeholder-like agent and folder names render intact")
        check("var savedDefaults = {};" in page and "@@CWDS@@" in page,
              "page placeholders are all substituted once")

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
    check(got == ("grok-4.7", "high", False), "empty cache: a named model is accepted (got %r)" % (got,))
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

    # A cached list whose lookup reports an error checks nothing: not listed.
    real_list = erg.grok_models_list
    erg.grok_models_list = lambda: ([], "grok models failed")
    try:
        got = erg.checked_model_effort("grok-new", "low")
    except ValueError as exc:
        got = "ValueError: %s" % exc
    erg.grok_models_list = real_list
    check(got == ("grok-new", "low", False),
          "cached list + lookup error: model reported as not listed (got %r)" % (got,))

    # `last` keeps the previous model when the new one was only format-checked.
    class Fake:
        data = {"last": {"model": "grok-4.7", "effort": "high"}}

        def _change_config(self, fn):
            fn(self.data)
    fake = Fake()
    erg.Handler._remember_last(fake, "grok-nope", "low", False)
    check(fake.data["last"] == {"model": "grok-4.7", "effort": "low"},
          "unverified model is not saved as last (got %r)" % (fake.data["last"],))
    erg.Handler._remember_last(fake, "grok-4.5", "xhigh", True, {"model"})
    check(fake.data["last"] == {"model": "grok-4.5", "effort": "low"},
          "only touched fields are remembered as last (got %r)" % (fake.data["last"],))
    erg.Handler._remember_last(fake, "grok-4.6", "low", True)
    check(fake.data["last"] == {"model": "grok-4.6", "effort": "low"}, "listed model is saved as last")

    text = (ROOT / "ergosphere.py").read_text(encoding="utf-8")
    check("grok saves a model or effort you choose as its new default" in text,
          "new-session dialog notes that grok saves the choice as its default")

    check("savedDefaults.effort = payload.effort" in text and "savedDefaults.model = payload.model" in text,
          "after a launch the page's saved defaults follow what grok was started with")

    check('payload.touched.push("effort")' in text and 'payload.touched.push("model")' in text,
          "the dialog sends which fields the user changed")

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    check("default_reasoning_effort" in readme, "README FAQ explains grok saving the model and effort")

    if failures:
        print("%d failed" % len(failures))
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
