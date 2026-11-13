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
import fcntl
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
os.environ.pop("XDG_RUNTIME_DIR", None)
os.environ.pop("TMPDIR", None)
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


def other_server(sid, fake=None, at=0.0, blind=False):
    """A second Ergosphere process (same HOME, its own environment) that starts sid.

    It has XDG_RUNTIME_DIR and TMPDIR set and this one has neither, like a
    user unit next to a system unit: the lock must not depend on them.
    at: start time (epoch) for a race. blind: it cannot see this process's
    groks (a separate process view, like another PID namespace).
    """
    env = dict(os.environ, XDG_RUNTIME_DIR=os.path.join(TMP, "run"), TMPDIR=os.path.join(TMP, "othertmp"))
    other = popen([sys.executable, "-B", "-c",
                   "import importlib.util,sys,time\n"
                   "s=importlib.util.spec_from_file_location('e',sys.argv[1]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m)\n"
                   "if sys.argv[7] == '1': m.session_holder = lambda *a, **k: None\n"
                   "while time.time() < float(sys.argv[6]): time.sleep(0.001)\n"
                   "try: m.open_live(sys.argv[2], [sys.argv[3], '-c', sys.argv[4], '--resume', sys.argv[2]], sys.argv[5], 80, 24)\n"
                   "except Exception as exc: print('refused', exc, flush=True)\n"
                   "print('up', flush=True); time.sleep(30)\n",
                   str(ROOT / "ergosphere.py"), sid, GROK, fake or FAKE, CWD, repr(at), "1" if blind else "0"],
                  stdout=subprocess.PIPE, env=env)
    if not at:
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


def check_disjoint_envs(erg):
    # Two servers with nothing in common but HOME (one with ~/.cache unusable,
    # the other with its own TMPDIR and XDG_RUNTIME_DIR) start one session at
    # the same moment: exactly one grok, every time.
    home2 = os.path.join(TMP, "home-nocache")
    os.makedirs(home2)
    os.symlink(os.path.join(HOME, ".grok"), os.path.join(home2, ".grok"))  # same sessions
    with open(os.path.join(home2, ".cache"), "w") as fh:
        fh.write("not a folder\n")
    counts = []
    for n in (30, 31, 32):
        sid = new_sid(n)
        at = time.time() + 1.0
        other = other_server(sid, at=at)
        os.environ["HOME"] = home2
        try:
            while time.time() < at:
                time.sleep(0.001)
            try:
                erg.open_live(sid, fake_cmd(sid), CWD, 80, 24)
            except Exception:
                pass
        finally:
            os.environ["HOME"] = HOME
        time.sleep(0.5)
        counts.append(len(groks_for(sid)))
        os.kill(other.pid, signal.SIGKILL)
    if counts != [1, 1, 1]:
        return "lock: two servers in different environments started %r groks" % counts
    return None


def check_lock_writes_nothing(erg):
    # The lock is the session folder itself, opened read-only: nothing new in
    # ~/.grok and the folder's mtime does not move.
    sid = new_sid(33)
    folder = erg.session_dir_for(sid)
    root = os.path.join(HOME, ".grok")
    before = sorted(os.path.join(d, f) for d, ds, fs in os.walk(root) for f in fs + ds)
    mtime = os.stat(folder).st_mtime_ns
    fds = erg.acquire_session_lock(sid)
    taken = bool(fds) and lock_held(erg, sid)
    for fd in fds or []:
        os.close(fd)
    after = sorted(os.path.join(d, f) for d, ds, fs in os.walk(root) for f in fs + ds)
    if not taken or before != after or os.stat(folder).st_mtime_ns != mtime:
        return "lock: taken=%s, new entries %r, mtime moved %s" % (
            taken, sorted(set(after) - set(before)), os.stat(folder).st_mtime_ns != mtime)
    return None


def check_new_session_launch(erg):
    # A new session has no folder yet: its fresh id needs no lock, and the
    # launch still starts exactly one grok.
    sid = "%08d-0000-4000-8000-000000000000" % 34
    try:
        erg.open_live(sid, [GROK, "-c", FAKE, "--session-id", sid], CWD, 80, 24)
    except Exception as exc:
        return "lock: a new session did not launch: %s" % exc
    if len(groks_for(sid)) != 1:
        return "lock: a new session launch started %d groks" % len(groks_for(sid))
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


def check_lock_kept_while_grok_lives(erg):
    # Quit, but grok outlives the kill: the session lock stays until it exits.
    sid = new_sid(14)
    live, _ = erg.open_live(sid, [GROK, "-c", "import signal,time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(60)\n", "--resume", sid], CWD, 80, 24)
    time.sleep(0.3)
    real = erg.kill_proc
    erg.kill_proc = lambda proc: None  # stands in for a grok stuck past SIGKILL + 3 s
    try:
        live.shutdown()
    finally:
        erg.kill_proc = real
    held = lock_held(erg, sid)
    os.kill(live.proc.pid, signal.SIGKILL)
    released = wait_for(lambda: not lock_held(erg, sid), 5)
    if not held or not released:
        return "lock: released while grok still ran (%s) or kept after it exited (%s)" % (not held, not released)
    return None


def lock_held(erg, sid):
    """True when some process holds the flock on the session folder."""
    folder = erg.session_dir_for(sid)
    if folder is None:
        return False
    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(fd)
    return False


def check_orphan_race(erg):
    # Server 1 starts grok and dies (its lock goes, its grok stays): a start
    # here must still see that grok and refuse.
    sid = new_sid(16)
    hardy = "import signal,time\nsignal.signal(signal.SIGHUP, signal.SIG_IGN)\nwhile True: time.sleep(1)\n"
    other = other_server(sid, hardy)
    os.kill(other.pid, signal.SIGKILL)
    other.wait()
    time.sleep(0.3)
    try:
        erg.open_live(sid, fake_cmd(sid), CWD, 80, 24)
    except Exception:
        pass
    n = len(groks_for(sid))
    if n != 1:
        return "lock: a start after the other server died beside its grok made %d groks" % n
    return None


def check_bad_sid(erg):
    # The session id becomes part of a path: anything else is refused.
    for bad in ("../x", "a/../../x", "..", ""):
        try:
            erg.acquire_session_lock(bad)
            accepted = True
        except ValueError:
            accepted = False
        except Exception:
            accepted = True
        if accepted:
            return "lock: session id %r was not refused" % bad
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
    os.kill(live.proc.pid, signal.SIGKILL)
    gone = wait_for(lambda: sid not in erg.LIVE, 5)
    after = erg.session_state(sid, with_turn=False)["state"]
    if during != "stopping" or not gone or after != "stopped":
        return "stop: while grok outlived the kill state=%r, after exit LIVE cleared=%s state=%r" % (during, gone, after)
    return None


def check_new_session_locked(erg):
    # A new session has no folder at launch; once grok makes it, the folder is
    # locked, so a server that cannot see this grok cannot resume it.
    sid = "%08d-0000-4000-8000-000000000000" % 37
    erg.open_live(sid, [GROK, "-c", FAKE, "--session-id", sid], CWD, 80, 24)
    time.sleep(0.2)
    new_sid(37)  # grok writes its folder
    locked = wait_for(lambda: lock_held(erg, sid), 3)
    other = other_server(sid, blind=True)
    n = len(groks_for(sid))
    os.kill(other.pid, signal.SIGKILL)
    if not locked or n != 1:
        return "new session: folder locked=%s; Resume from a blind server made %d groks" % (locked, n)
    return None


def session_folder(sid):
    return os.path.join(HOME, ".grok", "sessions", CWD.replace("/", "%2F"), sid)


def check_new_session_taken(erg):
    # Another server locks a new session's folder the moment it appears,
    # before this server's waiter does: this server stops its own grok and
    # tells its pages why, so only one grok runs.
    sid = "%08d-0000-4000-8000-000000000000" % 38
    folder = session_folder(sid)
    other = popen([sys.executable, "-c",
                   "import fcntl,os,sys,time\n"
                   "d=sys.argv[1]\n"
                   "while not os.path.isdir(d): pass\n"
                   "fd=os.open(d, os.O_RDONLY | os.O_DIRECTORY)\n"
                   "fcntl.flock(fd, fcntl.LOCK_EX)\n"
                   "time.sleep(30)\n", folder])
    time.sleep(0.3)
    live, _ = erg.open_live(sid, [GROK, "-c", FAKE, "--session-id", sid], CWD, 80, 24)
    tab = Tab()
    live.attach(tab)
    os.makedirs(folder)
    stopped = wait_for(lambda: not groks_for(sid), 5)
    told = wait_for(lambda: any(op == 1 and b"taken by another Ergosphere server" in p for op, p in tab.frames), 2)
    os.kill(other.pid, signal.SIGKILL)
    if not stopped or not told or sid in erg.LIVE:
        return "new session taken: own grok stopped=%s, page told=%s, still listed=%s" % (stopped, told, sid in erg.LIVE)
    return None


def check_new_session_late_folder(erg):
    # The folder may appear late (slow disk): the waiter keeps polling, more
    # slowly, for as long as grok runs, and locks it when it comes.
    sid = "%08d-0000-4000-8000-000000000000" % 39
    erg.NEW_SESSION_FAST_SECS = 0.3  # switch to 1 s polling quickly
    erg.open_live(sid, [GROK, "-c", FAKE, "--session-id", sid], CWD, 80, 24)
    time.sleep(2.5)
    os.makedirs(session_folder(sid))
    locked = wait_for(lambda: lock_held(erg, sid), 3)
    if not locked:
        return "new session: a folder made 2.5 s after launch was never locked"
    return None


def check_new_session_lock_error(erg):
    # The waiter cannot lock the new folder (here: no read permission, as a
    # transient EMFILE would also do). It must not leave this grok running
    # unlocked: once the folder is readable again, a server that cannot see
    # this grok would resume it and two groks would write one session.
    sid = "%08d-0000-4000-8000-000000000000" % 40
    folder = session_folder(sid)
    live, _ = erg.open_live(sid, [GROK, "-c", FAKE, "--session-id", sid], CWD, 80, 24)
    tab = Tab()
    live.attach(tab)
    os.makedirs(folder, mode=0o000)
    time.sleep(0.5)
    os.chmod(folder, 0o755)
    stopped = wait_for(lambda: not groks_for(sid), 5)
    told = any(op == 1 and b'"ended"' in p for op, p in tab.frames)
    other = other_server(sid, blind=True)
    n = len(groks_for(sid))
    os.kill(other.pid, signal.SIGKILL)
    if not stopped or not told or n != 1:
        return "new session lock error: own grok stopped=%s, page told=%s; Resume from a blind server made %d groks" % (stopped, told, n)
    return None


def check_cpu_callers_share(erg):
    # Two pollers close together both see a busy grok's CPU use.
    busy = popen([sys.executable, "-c", "while True: pass"])
    erg._cpu_pct(busy.pid)
    time.sleep(1.3)
    first = erg._cpu_pct(busy.pid)
    second = erg._cpu_pct(busy.pid)  # another poller right after
    busy.kill()
    if first is None or second is None or second < 50:
        return "cpu: a second caller right after the first saw %r (first %r)" % (second, first)
    return None


def main():
    erg = load()
    erg.print = lambda *a, **k: None
    checks = (check_quit_tells_every_tab, check_no_resume_while_stopping, check_two_servers,
              check_conflict_everywhere,
              check_held_while_running_here, check_holder_labels, check_words, check_turn_without_inhibit,
              check_disjoint_envs, check_lock_writes_nothing, check_new_session_launch,
              check_one_proc_pass, check_lock_kept_while_grok_lives, check_cpu_callers_share,
              check_orphan_race, check_bad_sid, check_stopping_until_exit, check_new_session_locked,
              check_new_session_taken, check_new_session_late_folder, check_new_session_lock_error)
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
