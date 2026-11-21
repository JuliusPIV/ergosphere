"""Hide a session whose folder was deleted, and show it again (#208).

POST /hide {id, hidden} records the id in Ergosphere's own settings file only;
nothing under ~/.grok is written. A hidden session leaves the Older list (and
the list tag changes, so an open list re-renders); its chat page offers Unhide;
Settings lists it with Unhide. Two hides at once both persist. A second hide
changes nothing. Bad or unknown ids are refused.

Run: python3 -B tests/hide_test.py
Stdlib only, no network, no grok, no server socket: handlers are called directly.
"""
import hashlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
import types
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = tempfile.mkdtemp(prefix="ergo-hide-")
HOME = os.path.join(TMP, "home")
CONFIG = os.path.join(TMP, "ergosphere.json")
os.environ["HOME"] = HOME
os.environ["ERGOSPHERE_CONFIG"] = CONFIG
SESSIONS = os.path.join(HOME, ".grok", "sessions")


def load():
    spec = importlib.util.spec_from_file_location("ergosphere", ROOT / "ergosphere.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def saved_session(cwd, title):
    sid = str(uuid.uuid4())
    folder = os.path.join(SESSIONS, "t", sid)
    os.makedirs(folder)
    with open(os.path.join(folder, "summary.json"), "w") as fh:
        json.dump({"info": {"id": sid, "cwd": cwd}, "generated_title": title,
                   "updated_at": "2026-01-01T00:00:00Z"}, fh)
    return sid


def grok_tree_hash():
    # Every path and byte under ~/.grok: Hide must not touch any of it.
    digest = hashlib.sha256()
    for top, dirs, files in sorted(os.walk(os.path.join(HOME, ".grok"))):
        dirs.sort()
        for name in sorted(files):
            path = os.path.join(top, name)
            digest.update(path.encode())
            with open(path, "rb") as fh:
                digest.update(fh.read())
    return digest.hexdigest()


def handler(erg, server, path, body=None):
    out = {}
    h = types.SimpleNamespace(path=path, server=server)
    h._guard = lambda state_change=False, spawn_get=False: out.setdefault("guard", state_change) or True
    h._json_object = lambda: body
    h._json = lambda code, obj: out.update(code=code, obj=obj)
    h._html = lambda page: out.update(code=200, html=page)
    for name in ("_change_config", "_saved_config", "_hide"):
        if hasattr(erg.Handler, name):
            setattr(h, name, types.MethodType(getattr(erg.Handler, name), h))
    return h, out


def post(erg, server, path, body):
    h, out = handler(erg, server, path, body)
    erg.Handler.do_POST(h)
    return out


def get(erg, server, path):
    h, out = handler(erg, server, path)
    erg.Handler.do_GET(h)
    return out


def list_html(erg, server):
    return get(erg, server, "/").get("html", "")


def list_tag(erg, server):
    sessions = erg.load_sessions(None)[1]
    visible = getattr(erg, "visible_sessions", lambda rows, config: rows)(sessions, server.config)
    return erg.list_rows_tag(visible, owners={})


def saved_hidden():
    try:
        with open(CONFIG) as fh:
            return json.load(fh).get("hidden")
    except (OSError, ValueError):
        return None


def run(erg):
    os.makedirs(SESSIONS)
    server = types.SimpleNamespace(config=erg.check_config({}, CONFIG), cwd_defaults={},
                                   server_address=("127.0.0.1", 1), args=None)
    keep = os.path.join(TMP, "keep")
    os.makedirs(keep)
    gone_sid = saved_session(os.path.join(TMP, "deleted"), "hide me")
    other_sid = saved_session(os.path.join(TMP, "deleted-too"), "hide me too")
    keep_sid = saved_session(keep, "keep me")
    fails = []
    before = grok_tree_hash()
    tag_before = list_tag(erg, server)
    if "hide me" not in list_html(erg, server):
        fails.append("setup: the folder-missing session is not listed")

    page = get(erg, server, "/chat?resume=" + gone_sid).get("html", "")
    if 'id="hide"' not in page:
        fails.append("folder-missing chat page has no Hide button")

    got = post(erg, server, "/hide", {"id": gone_sid, "hidden": True})
    if got.get("code") != 200 or not (got.get("obj") or {}).get("ok"):
        fails.append("/hide answered %r %r" % (got.get("code"), got.get("obj")))
    if got.get("guard") is not True:
        fails.append("/hide did not get the Origin check (state_change)")
    if saved_hidden() != [gone_sid]:
        fails.append("settings file hidden is %r, not [the id]" % saved_hidden())
    html = list_html(erg, server)
    if "hide me</span>" in html or 'data-sid="%s"' % gone_sid in html:
        fails.append("hidden session still in the list")
    if "keep me" not in html:
        fails.append("hiding one session dropped another from the list")
    if list_tag(erg, server) == tag_before:
        fails.append("list tag did not change, an open list would not re-render")
    if grok_tree_hash() != before:
        fails.append("hide changed files under ~/.grok")

    page = get(erg, server, "/chat?resume=" + gone_sid).get("html", "")
    if "var hiddenNow = true;" not in page:
        fails.append("hidden session's chat page does not know it is hidden (no Unhide)")
    settings = get(erg, server, "/settings").get("html", "")
    if gone_sid not in settings:
        fails.append("Settings does not list the hidden session")

    mtime = config_mtime()
    post(erg, server, "/hide", {"id": gone_sid, "hidden": True})
    if saved_hidden() != [gone_sid] or config_mtime() != mtime:
        fails.append("a second hide changed the settings file: %r" % saved_hidden())

    for body, want in (({"id": "../x", "hidden": True}, 400), ({"id": gone_sid}, 400),
                       ({"id": str(uuid.uuid4()), "hidden": True}, 404)):
        got = post(erg, server, "/hide", body)
        if got.get("code") != want:
            fails.append("/hide %r answered %r, want %d" % (body, got.get("code"), want))
    if saved_hidden() != [gone_sid]:
        fails.append("a refused hide changed the settings file: %r" % saved_hidden())
    try:
        erg.check_config({"hidden": ["ok-id", "../bad"]}, "x")
        fails.append("check_config accepted a bad hidden id")
    except ValueError:
        pass

    # Two tabs hide at the same moment: CONFIG_LOCK keeps both.
    go = threading.Barrier(2)

    def hide(sid):
        go.wait()
        post(erg, server, "/hide", {"id": sid, "hidden": True})
    threads = [threading.Thread(target=hide, args=(s,)) for s in (other_sid, keep_sid)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if sorted(saved_hidden() or []) != sorted([gone_sid, other_sid, keep_sid]):
        fails.append("concurrent hides lost one: %r" % saved_hidden())

    for sid in (gone_sid, other_sid, keep_sid):
        got = post(erg, server, "/hide", {"id": sid, "hidden": False})
        if got.get("code") != 200:
            fails.append("unhide answered %r %r" % (got.get("code"), got.get("obj")))
    if saved_hidden() != []:
        fails.append("after unhide the settings file hidden is %r" % saved_hidden())
    if "hide me</span>" not in list_html(erg, server) and "hide me [folder missing]" not in list_html(erg, server):
        fails.append("unhidden session is not back in the list")
    if grok_tree_hash() != before:
        fails.append("hide or unhide changed files under ~/.grok")

    return fails


def config_mtime():
    try:
        return os.stat(CONFIG).st_mtime_ns
    except OSError:
        return None


def main():
    erg = load()
    erg.print = lambda *a, **k: None
    try:
        fails = run(erg)
    except Exception as exc:
        fails = ["%s: %s" % (type(exc).__name__, exc)]
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    for f in fails:
        print("FAIL: " + f)
    if fails:
        return 1
    print("PASS: Hide and Unhide are kept in Ergosphere's settings only; the list, chat page and Settings follow")
    return 0


if __name__ == "__main__":
    sys.exit(main())
