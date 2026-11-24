"""session_dir_for only returns folders inside the sessions root (#41 review).

Covers: an id with a trailing newline is rejected; a session folder that is a
symlink to a folder outside the sessions root is not returned (nor its
summary.json, updates.jsonl or events.jsonl); a normal session still resolves.

Run: python3 -B tests/session_dir_guard_test.py
Stdlib only, no network, no server.
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = tempfile.mkdtemp(prefix="ergo-sdir-")
HOME = os.path.join(TMP, "home")
GROUP = os.path.join(HOME, ".grok", "sessions", "%2Fproj")
OUTSIDE = os.path.join(TMP, "outside")
os.makedirs(GROUP)
os.makedirs(OUTSIDE)
os.environ["HOME"] = HOME


def load():
    spec = importlib.util.spec_from_file_location("ergosphere", ROOT / "ergosphere.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check(name, ok, detail=""):
    print("%s %s %s" % ("ok  " if ok else "FAIL", name, detail))
    return ok


def main():
    ergo = load()
    results = []

    # A folder really named "nl\n" exists, so only the id check can refuse it.
    os.makedirs(os.path.join(GROUP, "nl\n"))
    got = ergo.session_dir_for("nl\n")
    results.append(check("trailing newline id rejected", got is None, repr(got)))

    with open(os.path.join(OUTSIDE, "summary.json"), "w") as fh:
        json.dump({"info": {"id": "escape"}}, fh)
    with open(os.path.join(OUTSIDE, "updates.jsonl"), "w") as fh:
        fh.write("{}\n")
    with open(os.path.join(OUTSIDE, "events.jsonl"), "w") as fh:
        fh.write(json.dumps({"type": "turn_started", "ts": "2099-01-01T00:00:00.000Z"}) + "\n")
    os.symlink(OUTSIDE, os.path.join(GROUP, "escape"))
    got = ergo.session_dir_for("escape")
    results.append(check("symlink outside root not returned", got is None, repr(got)))
    got = ergo.session_updates_path("escape")
    results.append(check("symlink outside root: no updates path", got is None, repr(got)))
    got = ergo.find_session("escape")
    results.append(check("symlink outside root: no summary", got is None, repr(got)))
    got = ergo._turn_in_events("escape", os.getpid())
    results.append(check("symlink outside root: no turn read", got is False, repr(got)))

    good = os.path.join(GROUP, "good-1")
    os.makedirs(good)
    with open(os.path.join(good, "summary.json"), "w") as fh:
        json.dump({"info": {"id": "good-1"}}, fh)
    got = ergo.session_dir_for("good-1")
    results.append(check("normal session resolves",
                         got is not None and os.path.samefile(got, good), repr(got)))
    row = ergo.find_session("good-1")
    results.append(check("normal session summary found", bool(row) and row["id"] == "good-1", repr(row)))
    return all(results)


if __name__ == "__main__":
    try:
        ok = main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(0 if ok else 1)
