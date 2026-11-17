"""One session state: who runs a session, and refusals that keep it to one grok.

Covers: every tab gets "ended" when one tab quits; no Resume while the last
grok is still exiting; no start while another grok on this host (a CLI, another
server's, one a stopped server left) holds the session; a session this server
runs stays attachable beside such a grok and is the grok its state names;
the chat status keeps the holder warning; holder labels and which holders
may be quit; the words on the pages; Settings counts a stopping grok, the
list marks it, and Restart is refused until it has exited.

Run: python3 -B tests/session_state_test.py
Stdlib only, no network, no real grok: "grok" is python under that name.
The chat status check runs the page script in node and is skipped without it.
"""
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import types
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = tempfile.mkdtemp(prefix="ergo-state-")
HOME = os.path.join(TMP, "home")
BIN = os.path.join(TMP, "bin")
GROK = os.path.join(BIN, "grok")
CWD = os.path.join(HOME, "proj")
os.makedirs(BIN)
os.makedirs(CWD)
os.symlink(sys.executable, GROK)
os.environ["HOME"] = HOME
# A fake grok: exits 0.8 s after SIGTERM, like a real one finishing up.
FAKE = ("import signal,sys,time\n"
        "signal.signal(signal.SIGTERM, lambda *a: (time.sleep(0.8), sys.exit(0)))\n"
        "while True: time.sleep(1)\n")
PROCS = []
RUN = str(uuid.uuid4())[9:]


def load():
    spec = importlib.util.spec_from_file_location("ergosphere", ROOT / "ergosphere.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def sid_for(n):
    # The holder scan sees every grok on the host: ids differ per run, so
    # another run of this test at the same time is not taken for a holder.
    return "%08d-%s" % (n, RUN)


def new_sid(n):
    sid = sid_for(n)
    folder = os.path.join(HOME, ".grok", "sessions", CWD.replace("/", "%2F"), sid)
    os.makedirs(folder)
    with open(os.path.join(folder, "summary.json"), "w") as fh:
        json.dump({"id": sid, "cwd": CWD, "title": "t%d" % n}, fh)
    return sid


def fake_cmd(sid):
    return [GROK, "-c", FAKE, "--resume", sid]


def popen(argv, **kw):
    kw.setdefault("stdout", subprocess.DEVNULL)
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
    PROCS.append(proc)
    return proc


def groks_for(sid):
    out = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            raw = open("/proc/%s/cmdline" % name, "rb").read()
            state = open("/proc/%s/stat" % name).read().rsplit(")", 1)[1].split()[0]
        except OSError:
            continue
        if state != "Z" and raw.startswith(GROK.encode() + b"\0") and sid.encode() in raw.split(b"\0"):
            out.append(int(name))
    return out


def wait_for(fn, secs=5):
    end = time.time() + secs
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.05)
    return False


class Tab:
    """A /term client as Live.attach sees it."""

    def __init__(self):
        self.frames = []

    def _send_frame(self, opcode, payload):
        self.frames.append((opcode, payload))

    def ended(self):
        return any(op == 1 and b'"ended"' in p for op, p in self.frames)


def reply_of(erg, method, body):
    got = {}
    handler = types.SimpleNamespace(
        _json_object=lambda: body,
        _json=lambda code, obj: got.update(code=code, obj=obj),
    )
    getattr(erg.Handler, method)(handler)
    return got


def check_quit_tells_every_tab(erg):
    sid = new_sid(1)
    live, _ = erg.open_live(sid, fake_cmd(sid), CWD, 80, 24)
    a, b = Tab(), Tab()
    live.attach(a)
    live.attach(b)
    time.sleep(0.3)
    live.shutdown()  # tab A pressed Quit
    with erg.LIVE_LOCK:
        erg.LIVE.pop(sid, None)
    if not wait_for(b.ended, 3):
        return "SM-1: Quit in tab A did not send ended to tab B"
    return None


def check_no_resume_while_stopping(erg):
    sid = new_sid(2)
    live, _ = erg.open_live(sid, fake_cmd(sid), CWD, 80, 24)
    time.sleep(0.3)
    stopper = threading.Thread(target=live.shutdown)
    stopper.start()
    time.sleep(0.2)
    refused = False
    try:
        erg.open_live(sid, fake_cmd(sid), CWD, 80, 24)
    except Exception as exc:  # SessionBusy on the fixed code
        refused = type(exc).__name__ == "SessionBusy"
    two = len(groks_for(sid)) > 1
    stopper.join(10)
    with erg.LIVE_LOCK:
        if erg.LIVE.get(sid) is live:
            erg.LIVE.pop(sid, None)
    if two or not refused:
        return "SM-2: Resume while the last grok was exiting started a second grok"
    erg.open_live(sid, fake_cmd(sid), CWD, 80, 24)  # once it exited, Resume works
    if len(groks_for(sid)) != 1:
        return "SM-2: Resume after the grok exited did not start exactly one grok"
    return None


def other_server(sid, fake=FAKE):
    """A second Ergosphere process (same HOME) that starts sid, then idles."""
    other = popen([sys.executable, "-B", "-c",
                   "import importlib.util,sys,time\n"
                   "s=importlib.util.spec_from_file_location('e',sys.argv[1]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m)\n"
                   "try: m.open_live(sys.argv[2], [sys.argv[3], '-c', sys.argv[4], '--resume', sys.argv[2]], sys.argv[5], 80, 24)\n"
                   "except Exception as exc: print('refused', exc, flush=True)\n"
                   "print('up', flush=True); time.sleep(30)\n",
                   str(ROOT / "ergosphere.py"), sid, GROK, fake, CWD],
                  stdout=subprocess.PIPE)
    other.stdout.readline()
    return other


def check_two_servers(erg):
    # The other server's grok is running and in view: a start here is refused.
    sid = new_sid(3)
    other = other_server(sid)
    refused = False
    try:
        erg.open_live(sid, fake_cmd(sid), CWD, 80, 24)
    except Exception as exc:
        refused = type(exc).__name__ == "SessionBusy"
    n = len(groks_for(sid))
    os.kill(other.pid, signal.SIGKILL)
    if n > 1 or not refused:
        return "SM-3: a start while another server's grok runs the session made %d groks" % n
    return None


def take_reply(erg, query):
    got = {}
    handler = types.SimpleNamespace(
        path="/take?" + query,
        _drain_body=lambda: None,
        _json=lambda code, obj: got.update(code=code, obj=obj),
    )
    erg.Handler._take(handler)
    return got


def check_take_over_other_server(erg):
    # Another server (listening on a port) runs the session. This page names
    # it with its address and offers to take it over; plain /take still
    # refuses; /take?server=1 quits only that grok, the other server lives
    # on, and Resume here then starts the one grok.
    sid = new_sid(51)
    other = popen([sys.executable, "-B", "-c",
                   "import importlib.util,socket,sys,time\n"
                   "s=importlib.util.spec_from_file_location('e',sys.argv[1]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m)\n"
                   "m.print=lambda *a, **k: None\n"
                   "l=socket.socket(); l.bind(('127.0.0.1', 0)); l.listen(1)\n"
                   "m.open_live(sys.argv[2], [sys.argv[3], '-c', sys.argv[4], '--resume', sys.argv[2]], sys.argv[5], 80, 24)\n"
                   "print(l.getsockname()[1], flush=True); time.sleep(30)\n",
                   str(ROOT / "ergosphere.py"), sid, GROK, FAKE, CWD],
                  stdout=subprocess.PIPE)
    port = int(other.stdout.readline())
    wait_for(lambda: groks_for(sid), 5)
    theirs = groks_for(sid)
    holder = erg.session_holder(sid)
    page = erg.chat_page(sid, holder, False, "")
    where = "127.0.0.1:%d" % port
    label = getattr(erg, "TAKEOVER_LABEL", "Take over here")
    if not holder or holder.get("kind") != "web" or where not in page or label not in page:
        other.kill()
        return "take-over: page for a session another server runs names %r, offers take-over %s, holder %r" % (
            where if where in page else None, label in page, holder)
    plain = take_reply(erg, "id=" + sid)
    if plain.get("code") != 409 or groks_for(sid) != theirs:
        other.kill()
        return "take-over: plain /take signalled another server's grok: %r" % plain
    got = take_reply(erg, "id=%s&server=1" % sid)
    gone = wait_for(lambda: not groks_for(sid), 3)
    alive = other.poll() is None
    try:
        _live, again = erg.open_live(sid, fake_cmd(sid), CWD, 80, 24)
        resumed = "started" if not again else "reattached"
    except Exception as exc:
        resumed = "%s: %s" % (type(exc).__name__, exc)
    n = len(groks_for(sid))
    other.kill()
    if got.get("code") != 200 or not gone or not alive or resumed != "started" or n != 1:
        return "take-over: /take?server=1 %r, their grok gone %s, other server alive %s, resume %r, groks %d" % (
            got, gone, alive, resumed, n)
    return None


def check_conflict_everywhere(erg):
    # This server runs the session and a CLI `grok -r` opens it too: the one
    # state says held, and the list marks the row, not a plain Idle row.
    sid = new_sid(35)
    erg.open_live(sid, fake_cmd(sid), CWD, 80, 24)
    popen([GROK, "-c", FAKE, "-r", sid])
    time.sleep(0.4)
    state = erg.session_state(sid, with_turn=False)
    rows = erg.dashboard_rows([{"id": sid, "title": "t", "cwd": CWD, "updated": ""}])
    row = [line for line in rows.split("\n") if 'data-sid="%s"' % sid in line]
    marked = bool(row) and "also open in a CLI" in row[0]
    if state["state"] != "held-by-cli" or not marked:
        return "conflict: here + CLI reads %r, list row marked %s" % (state["state"], marked)
    return None


def term_frames(erg, sid):
    """What a fresh tab's /term is sent, up to where it would read input."""
    frames = []

    def nothing(*args, **kwargs):
        return None

    handler = types.SimpleNamespace(
        _resolve_attach=lambda: (sid, 80, 24),
        headers={"Sec-WebSocket-Key": "k", "Sec-WebSocket-Version": "13"},
        send_response=nothing, send_header=nothing, end_headers=nothing, _json=nothing,
        connection=types.SimpleNamespace(settimeout=nothing, setsockopt=nothing, shutdown=nothing),
        _send_frame=lambda opcode, payload: frames.append((opcode, payload)),
        _read_input=nothing,
    )
    erg.Handler._term(handler)
    return frames


def chat_html(erg, sid):
    got = []
    handler = types.SimpleNamespace(path="/chat?resume=" + sid, _guard=lambda *a: True, _html=got.append)
    erg.Handler.do_GET(handler)
    return got[0]


def check_held_session(erg):
    # A stopped session that a CLI `grok -r <id>` holds: Resume starts nothing.
    sid = new_sid(4)
    popen([GROK, "-c", FAKE, "-r", sid])
    time.sleep(0.4)
    got = reply_of(erg, "_resume", {"id": sid})
    if got.get("code") != 409 or not got["obj"].get("held") or len(groks_for(sid)) != 1:
        return "SM-4: Resume while a CLI holds the session was not refused: %r" % got
    # This server runs a session and a CLI opens it too: a fresh tab still
    # attaches to this server's grok (to use it or Quit it) and Resume
    # answers as for any session running here. No third grok.
    sid = new_sid(41)
    erg.open_live(sid, fake_cmd(sid), CWD, 80, 24)
    popen([GROK, "-c", FAKE, "-r", sid])
    time.sleep(0.4)
    got = reply_of(erg, "_resume", {"id": sid})
    attached = any(op == 1 and b'"session"' in p for op, p in term_frames(erg, sid))
    page = chat_html(erg, sid)
    connects = "var running = true;" in page and "var hold = false;" in page
    if got.get("code") != 200 or not attached or not connects or len(groks_for(sid)) != 2:
        return "own grok beside a CLI: /resume %r, /term attached %s, chat page connects %s, groks %d" % (
            got.get("code"), attached, connects, len(groks_for(sid)))
    return None


def check_own_grok_named_beside_holder(erg):
    # This server runs a session and a CLI opens it too: the state names
    # this server's grok (pid, turn); the CLI shows only as the holder.
    sid = new_sid(43)
    erg.open_live(sid, fake_cmd(sid), CWD, 80, 24)
    mine = erg.LIVE[sid].proc.pid
    popen([GROK, "-c", FAKE, "-r", sid])
    time.sleep(0.4)
    st = erg.session_state(sid)
    holder = st.get("holder") or {}
    if st.get("pid") != mine or not holder.get("pid") or holder.get("pid") == mine:
        return "own grok beside a CLI: state pid %r (own %r), holder %r" % (st.get("pid"), mine, holder)
    return None


def check_orphan_notice_needs_quit(erg):
    # An orphan seen only through an open file cannot be quit: no button promised.
    text = erg.busy_notice(sid_for(44), {"pid": os.getpid(), "via": "openfile", "kind": "orphan"})
    if "A button can quit" in text or erg.ORPHAN_BUTTON_LABEL in text:
        return "orphan notice promises a quit button for a holder that cannot be quit"
    return None


STATUS_JS = r"""
var el = {textContent: "", style: {}};
var document = {getElementById: function () { return el; }};
var hold = false, ended = false, stopping = false, sawOutput = true, sessionReady = true;
var waitGen = 0, waitTimer = null;
function showEnded() { el.textContent = "Ended"; }
%s
%s
var out = [];
applyState({state: "held-by-cli", here: true, turn: "idle", holder: {pid: 7, kind: "cli"}});
markOutput(); out.push(el.textContent);
applyState({state: "running-idle", here: true, turn: "idle", holder: null});
markOutput(); out.push(el.textContent);
console.log(JSON.stringify(out));
"""


def check_status_keeps_warning(erg):
    # Output frames must not wipe the "also open in another grok" warning;
    # it goes when a state reply shows no holder.
    node = shutil.which("node")
    if not node:
        print("note: no node, chat status check skipped")
        return None
    page = erg.chat_page(sid_for(45), None, True, "")
    a = page[page.index("function setStatus"):page.index("function setTitle")]
    b = page[page.index("var runText"):page.index("function pollTitle")]
    c = page[page.index("function markOutput"):page.index("function armOutputWait")]
    js = STATUS_JS % (a + b, c)
    out = subprocess.run([node, "-e", js], capture_output=True, text=True, timeout=20)
    try:
        got = json.loads(out.stdout)
    except ValueError:
        return "chat status: node failed: %s" % out.stderr.strip()[-300:]
    if "also open in another grok" not in got[0] or got[1] != "Running":
        return "chat status: after output with a holder %r, after the holder left %r" % tuple(got)
    return None


FOCUS_JS = r"""
// A stand-in for the list: setting innerHTML replaces every control with a
// new object and drops focus, as a browser does.
function mk(tag, attrs, parent) {
  var n = {tagName: tag, parentNode: parent || null, getAttribute: function (k) {
    return attrs.hasOwnProperty(k) ? attrs[k] : null; }};
  n.focus = function () { document.activeElement = n; };
  return n;
}
function build() {
  var head = mk("DIV", {"data-section": "idle"});
  return [mk("BUTTON", {"data-col": "title"}, head),
          mk("A", {"data-kind": "session", "data-sid": "s1"}),
          mk("A", {"data-kind": "session", "data-sid": "s2"}),
          mk("A", {"data-kind": "more"})];
}
var nodes = build();
var rows = {contains: function (n) { return nodes.indexOf(n) !== -1; }};
Object.defineProperty(rows, "innerHTML", {set: function () {
  if (rows.contains(document.activeElement)) document.activeElement = null;
  nodes = build(); }});
var document = {activeElement: null,
  getElementById: function () { return rows; },
  querySelectorAll: function () { return nodes; }};
var selectedEl = null;
function applySort() {} function applyFilter() {} function sync() {}
%s
var out = [];
[0, 2, 3].forEach(function (i) {
  var was = nodes[i];
  was.focus();
  swapRows("x");
  out.push(document.activeElement === nodes[i] && nodes[i] !== was);
});
var outside = mk("A", {});
document.activeElement = outside;
swapRows("x");
out.push(document.activeElement === outside);
console.log(JSON.stringify(out));
"""


def check_refresh_keeps_focus(erg):
    # The 4 s list refresh replaces the rows; keyboard focus on a sort button,
    # a session link or "load more" must land on its replacement.
    node = shutil.which("node")
    if not node:
        print("note: no node, list focus check skipped")
        return None
    page = erg.dashboard_page([], [], {})
    js = FOCUS_JS % page[page.index("var refreshing = false;"):page.index("function refreshRows")]
    out = subprocess.run([node, "-e", js], capture_output=True, text=True, timeout=20)
    try:
        got = json.loads(out.stdout)
    except ValueError:
        return "list focus: node failed: %s" % out.stderr.strip()[-300:]
    if got != [True, True, True, True]:
        return "list focus: after a refresh, focus kept on [sort button, session, load more, outside] = %r" % got
    return None


def check_holder_labels(erg):
    sid = new_sid(5)
    stub = os.path.join(TMP, "srv", "ergosphere.py")
    os.makedirs(os.path.dirname(stub))
    with open(stub, "w") as fh:
        # A server whose grok runs a shell that runs `grok -r <id>`.
        # The shell's own command line does not name the session.
        fh.write("import os,subprocess,sys\n"
                 "env = dict(os.environ, G=sys.argv[1], F=sys.argv[2], S=sys.argv[3])\n"
                 "inner = \"import os,subprocess; e=os.environ; subprocess.call([e['G'], '-c', e['F'], '-r', e['S']])\"\n"
                 "subprocess.call([sys.argv[1], '-c', inner], env=env)\n")
    popen([sys.executable, "-B", stub, GROK, FAKE, sid])
    wait_for(lambda: groks_for(sid), 5)
    inner = max(groks_for(sid))
    kind = erg.holder_kind(inner)
    if kind != "cli":
        return "SM-10a: a CLI grok started inside a web session's shell is labelled %r" % kind
    # It is named a CLI, but an Ergosphere server is above it: the page
    # offers no button for it and /take does not signal it.
    holder = erg.session_holder(sid)
    offered = erg.holder_can_quit(holder)
    quit_ok, _info = erg.release_holder(sid)
    quit_ok = quit_ok or take_reply(erg, "id=%s&server=1" % sid).get("code") == 200
    if offered or quit_ok or inner not in groks_for(sid):
        return "take: a grok under another server's grok was offered for quitting %s, /take quit it %s" % (
            offered, quit_ok)
    # A grok its server left behind: session leader, no terminal, re-parented
    # to init or to whatever subreaper is nearest (here: one that is not systemd).
    for n, reaper in ((6, False), (8, True)):
        sid = new_sid(n)
        left = popen([sys.executable, "-B", "-c",
                      "import ctypes,os,sys,time\n"
                      "reaper = sys.argv[1] == '1'\n"
                      "if reaper: ctypes.CDLL(None).prctl(36, 1, 0, 0, 0)  # PR_SET_CHILD_SUBREAPER\n"
                      "if os.fork() == 0:\n"
                      "    if os.fork() == 0:\n"
                      "        os.setsid(); os.execv(sys.argv[2], sys.argv[2:])\n"
                      "    os._exit(0)\n"
                      "os.wait()\n"
                      "if reaper: time.sleep(30)\n",
                      "1" if reaper else "0", GROK, "-c", FAKE, "--resume", sid])
        wait_for(lambda: groks_for(sid), 5)
        pid = groks_for(sid)[0]
        adopted = wait_for(lambda: (erg.proc_stat_ppid(pid) == left.pid) == reaper, 5)
        holder = erg.session_holder(sid)
        kind = holder and holder.get("kind")
        os.kill(pid, signal.SIGKILL)
        if not adopted or kind != "orphan":
            return "SM-10b: a grok left by a stopped server (under a plain subreaper: %s, adopted as set up: %s) is labelled %r" % (
                reaper, adopted, kind)
    return None


def check_words(erg):
    chat = erg.chat_page("00000001-0000-4000-8000-000000000000", None, True, "")
    if 'setStatus("Active"' in chat or '"Working now"' not in chat or 'runText = "Running"' not in chat:
        return "SM-14: chat status is not Running / Working now"
    rows = erg.dashboard_page([], [], {})
    if "Working now: " not in rows or "Active Sessions" in rows or "/list-rows" not in rows:
        return "SM-14/15: list does not say Working now or does not refresh itself"
    settings = erg.settings_page({"bind": "", "port": "", "runningBind": "", "runningPort": 1, "binds": [],
                                  "agents": {}, "names": [], "sessions": 0, "overrides": []})
    if "Running in this server: " not in settings:
        return "SM-14: Settings does not say Running in this server"
    return None


def check_new_session_launch(erg):
    # A new session has no folder yet: the launch starts exactly one grok.
    sid = sid_for(34)
    try:
        erg.open_live(sid, [GROK, "-c", FAKE, "--session-id", sid], CWD, 80, 24)
    except Exception as exc:
        return "new session: did not launch: %s" % exc
    if len(groks_for(sid)) != 1:
        return "new session: the launch started %d groks" % len(groks_for(sid))
    return None


def check_one_proc_pass(erg):
    # The list's state lookup reads /proc once, not once per live session.
    for n in (10, 11, 12):
        sid = new_sid(n)
        erg.open_live(sid, fake_cmd(sid), CWD, 80, 24)
        popen([GROK, "-c", FAKE, "-r", new_sid(n + 10)])
    time.sleep(0.3)
    calls = []
    real = os.listdir

    def counting(path="."):
        if path == "/proc":
            calls.append(path)
        return real(path)
    os.listdir = counting
    try:
        states = erg.list_states()
    finally:
        os.listdir = real
    if len(states) < 6 or len(calls) != 1:
        return "cost: list_states saw %d sessions with %d /proc passes" % (len(states), len(calls))
    return None


HARDY = "import signal,time\nsignal.signal(signal.SIGHUP, signal.SIG_IGN)\nwhile True: time.sleep(1)\n"


def check_left_behind_grok(erg):
    # A server starts grok and dies, its grok stays: a start here is refused.
    sid = new_sid(16)
    other = other_server(sid, HARDY)
    os.kill(other.pid, signal.SIGKILL)
    other.wait()
    time.sleep(0.3)
    try:
        erg.open_live(sid, fake_cmd(sid), CWD, 80, 24)
    except Exception:
        pass
    n = len(groks_for(sid))
    if n != 1:
        return "left-behind grok: a start after its server died made %d groks" % n
    return None


def check_restart_counts_stopping(erg):
    # Quit was pressed and grok is still exiting: Settings still counts it
    # and Restart is refused, as for any grok this server runs.
    sid = new_sid(37)
    live, _ = erg.open_live(sid, fake_cmd(sid), CWD, 80, 24)
    time.sleep(0.3)
    before = erg.running_sessions()
    stopper = threading.Thread(target=live.shutdown)
    stopper.start()
    time.sleep(0.2)
    during = erg.running_sessions()
    got = {}
    try:
        erg.Handler._settings_restart(types.SimpleNamespace(_json=lambda code, obj: got.update(code=code, obj=obj)))
    except AttributeError:
        pass  # the gate let it through and went on to read the server's settings
    finally:
        allowed = erg.RESTART.is_set()
        erg.RESTART.clear()
    stopper.join(10)
    with erg.LIVE_LOCK:
        if erg.LIVE.get(sid) is live:
            erg.LIVE.pop(sid, None)
    refused = (got.get("obj") or {}).get("sessions")
    if during != before or allowed or got.get("code") != 409 or refused != before:
        return "restart: with a grok still exiting Settings counted %d of %d, Restart allowed=%s, reply %r" % (
            during, before, allowed, got)
    return None


def check_stopping_until_exit(erg):
    # Quit from a tab, grok outlives the kill: the state stays stopping (not
    # held by some other grok) until grok exits, then reads stopped.
    sid = new_sid(36)
    live, _ = erg.open_live(sid, [GROK, "-c", "import signal,time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(60)\n", "--resume", sid], CWD, 80, 24)
    time.sleep(0.3)
    real = erg.kill_proc
    erg.kill_proc = lambda proc: None  # stands in for a grok stuck past SIGKILL + 3 s
    try:
        try:
            erg.Handler._on_text(None, live, b'{"type":"stop"}')
        except EOFError:
            pass
    finally:
        erg.kill_proc = real
    during = erg.session_state(sid, with_turn=False)["state"]
    rows = erg.dashboard_rows([{"id": sid, "title": "t", "cwd": CWD, "updated": ""}])
    row = [line for line in rows.split("\n") if 'data-sid="%s"' % sid in line]
    marked = bool(row) and "[stopping]" in row[0]
    os.kill(live.proc.pid, signal.SIGKILL)
    gone = wait_for(lambda: sid not in erg.LIVE, 5)
    after = erg.session_state(sid, with_turn=False)["state"]
    if during != "stopping" or not gone or after != "stopped":
        return "stop: while grok outlived the kill state=%r, after exit LIVE cleared=%s state=%r" % (during, gone, after)
    if not marked:
        return "stop: the list row of a stopping session is not marked [stopping]: %r" % row
    return None


def main():
    erg = load()
    erg.print = lambda *a, **k: None
    checks = (check_quit_tells_every_tab, check_no_resume_while_stopping, check_two_servers,
              check_take_over_other_server, check_conflict_everywhere,
              check_held_session, check_own_grok_named_beside_holder,
              check_orphan_notice_needs_quit, check_status_keeps_warning, check_refresh_keeps_focus, check_holder_labels, check_words,
              check_new_session_launch, check_one_proc_pass,
              check_left_behind_grok, check_restart_counts_stopping, check_stopping_until_exit)
    failures = []
    try:
        for check in checks:
            try:
                failure = check(erg)
            except Exception as exc:
                failure = "%s: %s: %s" % (check.__name__, type(exc).__name__, exc)
            if failure:
                failures.append(failure)
    finally:
        erg.STOP.set()
        for live in list(erg.LIVE.values()):
            erg.kill_proc(live.proc)
        for proc in PROCS:
            try:
                proc.kill()
            except OSError:
                pass
        # Fake groks whose parent was killed (the second server, the orphan).
        for name in os.listdir("/proc"):
            try:
                raw = open("/proc/%s/cmdline" % name, "rb").read() if name.isdigit() else b""
                if raw.startswith(GROK.encode() + b"\0"):
                    os.kill(int(name), signal.SIGKILL)
            except OSError:
                pass
        shutil.rmtree(TMP, ignore_errors=True)
    for failure in failures:
        print("FAIL: " + failure)
    if failures:
        return 1
    print("PASS: one session state; no start beside a grok in view; holder labels and words")
    return 0


if __name__ == "__main__":
    sys.exit(main())
