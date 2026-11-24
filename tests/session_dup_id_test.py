"""find_session keeps looking past an incomplete folder with the same id (#41 review r2-1).

Two groups hold the same session id. The first group os.scandir visits has the
id folder without summary.json. The second has a valid summary. load_sessions
lists the session, so find_session must find it too, not return None (which the
server turns into HTTP 404 on resume).

The incomplete group is forced to be visited first, so the test fails on the
old code for the intended reason and does not depend on filesystem order.

Run: python3 -B tests/session_dup_id_test.py
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
TMP = tempfile.mkdtemp(prefix="ergo-dup-id-")
HOME = os.path.join(TMP, "home")
SESS = os.path.join(HOME, ".grok", "sessions")
os.makedirs(SESS)
os.environ["HOME"] = HOME
SID = "dup-1"


def load():
    spec = importlib.util.spec_from_file_location("ergosphere", ROOT / "ergosphere.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check(name, ok, detail=""):
    print("%s %s %s" % ("ok  " if ok else "FAIL", name, detail))
    return ok


def main():
    names = ["%%2Fg%d" % i for i in range(16)]
    for name in names:
        os.makedirs(os.path.join(SESS, name))
    first = next(entry.name for entry in os.scandir(SESS))
    second = next(name for name in names if name != first)
    os.makedirs(os.path.join(SESS, first, SID))
    cwd = os.path.join(TMP, "proj")
    os.makedirs(cwd)
    good = os.path.join(SESS, second, SID)
    os.makedirs(good)
    with open(os.path.join(good, "summary.json"), "w") as fh:
        json.dump({"info": {"id": SID, "cwd": cwd}, "updated_at": "2026-01-01T00:00:00Z"}, fh)

    ergo = load()
    results = []
    listed = [row["id"] for row in ergo.load_sessions()[1]]
    results.append(check("load_sessions lists the session", SID in listed, repr(listed)))
    row = ergo.find_session(SID)
    results.append(check("find_session skips the incomplete folder",
                         bool(row) and row["id"] == SID and row["cwd"] == cwd, repr(row)))
    return all(results)


if __name__ == "__main__":
    try:
        ok = main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(0 if ok else 1)
