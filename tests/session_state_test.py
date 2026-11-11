"""One session state: who runs a session, and refusals that keep it to one grok.

Covers: every tab gets "ended" when one tab quits; no Resume while the last
grok is still exiting; two servers cannot both start one session; a session
held by a CLI is refused even when this server runs it too; holder labels;
the words on the pages; mid-turn detection without systemd-inhibit.

Run: python3 -B tests/session_state_test.py
Stdlib only, no network, no real grok: "grok" is python under that name.
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
os.environ.pop("XDG_RUNTIME_DIR", None)  # lock folders: ~/.cache, then the temp dir
LOCK_TMP = os.path.join(TMP, "systmp")  # stands in for the system temp dir
os.makedirs(LOCK_TMP)
tempfile.tempdir = LOCK_TMP
# A fake grok: exits 0.8 s after SIGTERM, like a real one finishing up.
FAKE = ("import signal,sys,time\n"
        "signal.signal(signal.SIGTERM, lambda *a: (time.sleep(0.8), sys.exit(0)))\n"
        "while True: time.sleep(1)\n")
PROCS = []


def load():
    spec = importlib.util.spec_from_file_location("ergosphere", ROOT / "ergosphere.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def new_sid(n):
    sid = "%08d-0000-4000-8000-000000000000" % n
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


def other_server(sid):
    """A second Ergosphere process (same HOME and temp dir) that starts sid."""
    env = dict(os.environ, TMPDIR=LOCK_TMP)
    other = popen([sys.executable, "-B", "-c",
                   "import importlib.util,sys,time\n"
                   "s=importlib.util.spec_from_file_location('e',sys.argv[1]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m)\n"
                   "m.open_live(sys.argv[2], [sys.argv[3], '-c', sys.argv[4], '--resume', sys.argv[2]], sys.argv[5], 80, 24)\n"
                   "print('up', flush=True); time.sleep(30)\n",
                   str(ROOT / "ergosphere.py"), sid, GROK, FAKE, CWD], stdout=subprocess.PIPE, env=env)
    other.stdout.readline()
    return other


def check_two_servers(erg):
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
        return "SM-3: a second server started the same session (%d groks)" % n
    return None


def check_held_while_running_here(erg):
    sid = new_sid(4)
    erg.open_live(sid, fake_cmd(sid), CWD, 80, 24)
    popen([GROK, "-c", FAKE, "-r", sid])  # grok -r <id> typed in a shell
    time.sleep(0.4)
    got = reply_of(erg, "_resume", {"id": sid})
    if got.get("code") != 409 or not got["obj"].get("held"):
        return "SM-4: Resume while a CLI holds the session was not refused: %r" % got
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
    sid = new_sid(6)
    # A grok its server left behind: session leader, re-parented to init.
    popen([sys.executable, "-B", "-c",
           "import os,sys\n"
           "if os.fork() == 0:\n"
           "    os.setsid(); os.execv(sys.argv[1], sys.argv[1:])\n",
           GROK, "-c", FAKE, "--resume", sid]).wait()
    wait_for(lambda: groks_for(sid), 5)
    pid = groks_for(sid)[0]
    holder = erg.session_holder(sid)
    kind = holder and holder.get("kind")
    os.kill(pid, signal.SIGKILL)
    if kind != "orphan":
        return "SM-10b: a grok left by a stopped server is labelled %r" % kind
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


def check_turn_without_inhibit(erg):
    sid = new_sid(7)
    proc = popen([GROK, "-c", FAKE, "--resume", sid])
    erg.inhibit_works = lambda: False
    folder = erg.session_dir_for(sid)
    path = os.path.join(folder, "events.jsonl")
    with open(path, "w") as fh:
        fh.write("{}\n")
    if erg.grok_turn(proc.pid, sid) != "idle":
        return "25l: the writes grok makes while it starts read as a turn"
    erg.TURN_START_SECS = 0  # as if this write came later, mid-session
    working = erg.grok_turn(proc.pid, sid)
    old = time.time() - 600
    for name in os.listdir(folder):
        os.utime(os.path.join(folder, name), (old, old))
    idle = erg.grok_turn(proc.pid, sid)
    if (working, idle) != ("working", "idle"):
        return "25l: without systemd-inhibit a turn reads %r and a quiet session %r" % (working, idle)
    return None


def no_cache_home():
    """A HOME whose ~/.cache is a file: the first lock folder cannot be made."""
    home2 = os.path.join(TMP, "home-ro")
    if not os.path.isdir(home2):
        os.makedirs(home2)
        with open(os.path.join(home2, ".cache"), "w") as fh:
            fh.write("not a folder\n")
    return home2


def check_no_lock_folder(erg):
    # ~/.cache unusable: the lock moves to the next folder, the launch works.
    sid = new_sid(8)
    os.environ["HOME"] = no_cache_home()
    try:
        erg.open_live(sid, fake_cmd(sid), CWD, 80, 24)
    except Exception as exc:
        return "lock: a launch fails when ~/.cache cannot be used: %s" % exc
    finally:
        os.environ["HOME"] = HOME
    lock = os.path.join(LOCK_TMP, "ergosphere-%d" % os.getuid(), "locks", sid + ".lock")
    if len(groks_for(sid)) != 1 or not os.path.exists(lock):
        return "lock: without ~/.cache the launch did not use the temp-dir lock"
    return None


def check_two_servers_no_cache(erg):
    # Both servers fall back to the same folder, so one start still wins.
    sid = new_sid(9)
    os.environ["HOME"] = no_cache_home()
    try:
        other = other_server(sid)
        try:
            erg.open_live(sid, fake_cmd(sid), CWD, 80, 24)
        except Exception:
            pass
        n = len(groks_for(sid))
        os.kill(other.pid, signal.SIGKILL)
    finally:
        os.environ["HOME"] = HOME
    if n != 1:
        return "lock: two servers without ~/.cache started %d groks for one session" % n
    return None


def check_all_lock_folders_fail(erg):
    # Nowhere to lock: refuse with a plain message, start nothing.
    sid = new_sid(13)
    private = os.path.join(LOCK_TMP, "ergosphere-%d" % os.getuid())
    os.makedirs(private, exist_ok=True)
    os.chmod(private, 0o777)  # others could write it: not used
    os.environ["HOME"] = no_cache_home()
    try:
        erg.open_live(sid, fake_cmd(sid), CWD, 80, 24)
        message = ""
    except Exception as exc:
        message = str(exc)
    finally:
        os.environ["HOME"] = HOME
        os.chmod(private, 0o700)
    if groks_for(sid) or "no folder for session lock files" not in message or "Errno" in message:
        return "lock: with no usable lock folder the start was not refused plainly: %r" % message
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


def main():
    erg = load()
    erg.print = lambda *a, **k: None
    checks = (check_quit_tells_every_tab, check_no_resume_while_stopping, check_two_servers,
              check_held_while_running_here, check_holder_labels, check_words, check_turn_without_inhibit,
              check_no_lock_folder, check_two_servers_no_cache, check_all_lock_folders_fail,
              check_one_proc_pass)
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
    print("PASS: one session state; one grok per session; holder labels and words")
    return 0


if __name__ == "__main__":
    sys.exit(main())
