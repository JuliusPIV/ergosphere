"""A saved session whose folder was deleted reads folder-missing everywhere (#43).

The one session state names it, so /session-info, /resume, the list and the
chat page agree: /resume refuses it (409, nothing started) with the reason,
the list row is marked, the chat page explains instead of offering Resume.
A session still running when its folder goes stays attachable and reads
folder-missing only once its grok exits; a folder put back reads stopped.

Run: python3 -B tests/folder_missing_test.py
Stdlib only, no network, no real grok: "grok" is python under that name.
"""
import importlib.util
import json
import os
import shutil
import signal
import sys
import tempfile
import time
import types
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = tempfile.mkdtemp(prefix="ergo-gone-")
HOME = os.path.join(TMP, "home")
BIN = os.path.join(TMP, "bin")
GROK = os.path.join(BIN, "grok")
os.makedirs(BIN)
os.symlink(sys.executable, GROK)
os.environ["HOME"] = HOME
FAKE = "import time\nwhile True: time.sleep(1)\n"


def load():
    spec = importlib.util.spec_from_file_location("ergosphere", ROOT / "ergosphere.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def saved_session(cwd):
    # A fresh id per run: the holder scan sees every grok on the host.
    sid = str(uuid.uuid4())
    folder = os.path.join(HOME, ".grok", "sessions", "t", sid)
    os.makedirs(folder)
    with open(os.path.join(folder, "summary.json"), "w") as fh:
        json.dump({"info": {"id": sid, "cwd": cwd}, "generated_title": "gone test",
                   "updated_at": "2026-01-01T00:00:00Z"}, fh)
    return sid


def new_folder(name):
    path = os.path.join(TMP, name)
    os.makedirs(path)
    return path


def state(erg, sid):
    return erg.session_state(sid, with_turn=False)["state"]


def info(erg, sid):
    got = {}
    handler = types.SimpleNamespace(path="/session-info?id=" + sid,
                                    _json=lambda code, obj: got.update(code=code, obj=obj))
    erg.Handler._session_info(handler)
    return got.get("obj") or {}


def chat_html(erg, sid):
    got = []
    handler = types.SimpleNamespace(path="/chat?resume=" + sid, _guard=lambda *a: True, _html=got.append)
    erg.Handler.do_GET(handler)
    return got[0]


def row_line(erg, sid):
    rows = erg.load_sessions(None)[1]
    html = erg.dashboard_rows(rows, owners={})
    line = [x for x in html.split("\n") if 'data-sid="%s"' % sid in x]
    return line[0] if line else ""


def started(erg, sid):
    with erg.LIVE_LOCK:
        return sid in erg.LIVE or sid in erg.STARTING


def check_deleted_folder(erg):
    fails = []
    folder = new_folder("deleted")
    sid = saved_session(folder)
    before_tag = erg.list_rows_tag(erg.load_sessions(None)[1], owners={})
    if state(erg, sid) != "stopped" or "[folder missing]" in row_line(erg, sid):
        fails.append("folder present: state %r, row %r" % (state(erg, sid), row_line(erg, sid)))
    os.rmdir(folder)
    if state(erg, sid) != "folder-missing":
        fails.append("state of a deleted folder is %r, not folder-missing" % state(erg, sid))
    if info(erg, sid).get("state") != "folder-missing":
        fails.append("/session-info state is %r" % info(erg, sid).get("state"))
    code, body = erg.resume_reply(sid)
    if code != 409 or body.get("state") != "folder-missing" or folder not in body.get("error", ""):
        fails.append("/resume of a deleted folder: %r %r" % (code, body))
    if started(erg, sid):
        fails.append("/resume of a deleted folder started a grok")
    if "[folder missing]" not in row_line(erg, sid):
        fails.append("list row not marked: %r" % row_line(erg, sid))
    if erg.list_rows_tag(erg.load_sessions(None)[1], owners={}) == before_tag:
        fails.append("list tag did not change when the folder was deleted, the list would not re-render")
    page = chat_html(erg, sid)
    if '<span id="status">folder missing</span>' not in page or folder not in page:
        fails.append("chat page does not say the folder is missing")
    os.makedirs(folder)
    if state(erg, sid) != "stopped" or "[folder missing]" in row_line(erg, sid):
        fails.append("folder put back: state %r, row %r" % (state(erg, sid), row_line(erg, sid)))
    if 'var goneText = "";' not in chat_html(erg, sid):
        fails.append("folder put back: chat page still explains a missing folder")
    return fails


def check_deleted_during_resume(erg):
    # The folder goes after /resume read the state, before it starts grok.
    folder = new_folder("racing")
    sid = saved_session(folder)
    real = erg.settled_state

    def read_then_delete(want, with_turn=True):
        out = real(want, with_turn=with_turn)
        if want == sid and os.path.isdir(folder):
            os.rmdir(folder)
        return out

    erg.settled_state = read_then_delete
    try:
        code, body = erg.resume_reply(sid)
    finally:
        erg.settled_state = real
    if code != 409 or body.get("state") != "folder-missing" or started(erg, sid):
        return ["folder deleted mid-/resume: %r %r, started %s" % (code, body, started(erg, sid))]
    return []


def check_deleted_while_running(erg):
    folder = new_folder("running")
    sid = saved_session(folder)
    live, _ = erg.open_live(sid, [GROK, "-c", FAKE, "--resume", sid], folder, 80, 24)
    os.rmdir(folder)
    fails = []
    during = state(erg, sid)
    if during in ("stopped", "folder-missing"):
        fails.append("running session read %r after its folder went" % during)
    code, body = erg.resume_reply(sid)
    if code != 200:
        fails.append("running session with its folder gone is not attachable: %r %r" % (code, body))
    os.kill(live.proc.pid, signal.SIGKILL)
    end = time.time() + 5
    while time.time() < end and state(erg, sid) != "folder-missing":
        time.sleep(0.05)
    if state(erg, sid) != "folder-missing":
        fails.append("after its grok exited the session reads %r" % state(erg, sid))
    return fails


def check_blank_cwd_unchanged(erg):
    sid = saved_session("")
    if state(erg, sid) != "stopped":
        return ["a summary with no cwd reads %r (was stopped)" % state(erg, sid)]
    return []


def main():
    erg = load()
    erg.print = lambda *a, **k: None
    fails = []
    try:
        for check in (check_deleted_folder, check_deleted_during_resume,
                      check_deleted_while_running, check_blank_cwd_unchanged):
            try:
                fails += check(erg)
            except Exception as exc:
                fails.append("%s: %s: %s" % (check.__name__, type(exc).__name__, exc))
    finally:
        erg.STOP.set()
        for live in list(erg.LIVE.values()):
            erg.kill_proc(live.proc)
        shutil.rmtree(TMP, ignore_errors=True)
    for f in fails:
        print("FAIL: " + f)
    if fails:
        return 1
    print("PASS: a deleted folder reads folder-missing in the state, /resume, the list and the chat page")
    return 0


if __name__ == "__main__":
    sys.exit(main())
