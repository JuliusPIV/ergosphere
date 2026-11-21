"""The two polls (/session-info?id=, /list-rows) reuse one /proc pass while nothing changed (#42).

Covers: unchanged polls make at most one full /proc pass; the polls still see
at once a grok that starts, a wrapper that execs grok, a grok that opens the
session's file after a poll, a grok that quits, a
pid taken by another program (also one with the same argv, told apart by
parent and start time), a turn starting and ending, this server
starting and stopping its own grok, and a wrapper parent exiting (reparent);
concurrent polls agree; a slow poll scan holds no lock that actions need;
a /proc listing that fails mid-way; actions still make their own scan.

Run: python3 -B tests/poll_scan_test.py
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
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = tempfile.mkdtemp(prefix="ergo-poll-")
HOME = os.path.join(TMP, "home")
BIN = os.path.join(TMP, "bin")
GROK = os.path.join(BIN, "grok")
CWD = os.path.join(HOME, "proj")
os.makedirs(BIN)
os.makedirs(CWD)
os.symlink(sys.executable, GROK)
os.environ["HOME"] = HOME
FAKE = "import time\nwhile True: time.sleep(1)\n"
# A fake grok that runs a turn-mark child while the file argv[1] exists.
# The mark text is joined at run time so this grok's own argv does not carry it.
TURN = ("import os,subprocess,sys,time\n"
        "flag=sys.argv[1]; child=None\n"
        "while True:\n"
        "    if os.path.exists(flag) and child is None:\n"
        "        child=subprocess.Popen(['sh','-c','sleep 600','agent turn '+'in progress'])\n"
        "    if not os.path.exists(flag) and child is not None:\n"
        "        child.kill(); child.wait(); child=None\n"
        "    time.sleep(0.02)\n")
PROCS = []
RUN = str(uuid.uuid4())[9:]


def load():
    spec = importlib.util.spec_from_file_location("ergosphere", ROOT / "ergosphere.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def new_sid(n):
    # Ids differ per run: another run at the same time is not taken for a holder.
    sid = "%08d-%s" % (n, RUN)
    folder = os.path.join(HOME, ".grok", "sessions", CWD.replace("/", "%2F"), sid)
    os.makedirs(folder)
    with open(os.path.join(folder, "summary.json"), "w") as fh:
        json.dump({"id": sid, "cwd": CWD, "title": "t%d" % n}, fh)
    return sid


def popen(argv):
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    PROCS.append(proc)
    return proc


def cmdline(pid):
    try:
        return open("/proc/%d/cmdline" % pid, "rb").read()
    except OSError:
        return b""


def wait_for(fn, secs=10):
    end = time.monotonic() + secs
    while time.monotonic() < end:
        if fn():
            return True
        time.sleep(0.02)
    return False


def info(erg, sid):
    got = {}
    handler = types.SimpleNamespace(path="/session-info?id=" + sid,
                                    _json=lambda code, obj: got.update(code=code, obj=obj))
    erg.Handler._session_info(handler)
    return got["obj"]


def rows(erg, tag=""):
    out = {"head": {}, "body": b""}
    handler = types.SimpleNamespace(
        headers={"X-Rows-Tag": tag},
        send_response=lambda code: out.update(code=code),
        send_header=lambda k, v: out["head"].update({k: v}),
        end_headers=lambda: None,
        wfile=types.SimpleNamespace(write=lambda b: out.update(body=out["body"] + b)),
    )
    erg.Handler._list_rows(handler)
    return out


class Count:
    """Counts full /proc passes (ProcScan constructions)."""

    def __init__(self, erg):
        self.erg, self.n, self.real = erg, 0, erg.ProcScan
        count = self

        class Counting(self.real):
            def __init__(self):
                count.n += 1
                super().__init__()
        erg.ProcScan = Counting

    def done(self):
        self.erg.ProcScan = self.real


def expect(erg, sid, state):
    got = info(erg, sid)["state"]
    return None if got == state else "%s: poll says %s, want %s" % (sid, got, state)


def frozen_proc(erg):
    """Hold /proc still: other runs on this machine start and stop groks too."""
    real_listdir, real_cmd, real_ppid = os.listdir, erg.read_proc_cmdline, erg.proc_stat_ppid
    names = [n for n in real_listdir("/proc") if n.isdigit()]
    cmd = {int(n): real_cmd(int(n)) for n in names}
    ppid = {int(n): real_ppid(int(n)) for n in names}
    real_ident = getattr(erg, "_proc_ident", None)
    ident = {int(n): (str(ppid[int(n)]).encode(), b"1") for n in names}
    os.listdir = lambda path=".": list(names) if path == "/proc" else real_listdir(path)
    erg.read_proc_cmdline = lambda pid: cmd.get(int(pid), b"")
    erg.proc_stat_ppid = lambda pid: ppid.get(int(pid))
    if real_ident is not None:
        erg._proc_ident = lambda pid: ident.get(int(pid))

    def undo():
        os.listdir, erg.read_proc_cmdline, erg.proc_stat_ppid = real_listdir, real_cmd, real_ppid
        if real_ident is not None:
            erg._proc_ident = real_ident
    return undo


def check_unchanged_polls_reuse_one_pass(erg):
    # The issue: every poll scanned all of /proc. Unchanged: one pass for six polls.
    sid = new_sid(1)
    popen([GROK, "-c", FAKE, "-r", sid])
    if not wait_for(lambda: info(erg, sid)["state"] == "held-by-cli"):
        return "cost: holder never seen"
    undo = frozen_proc(erg)
    count = Count(erg)
    try:
        tag = rows(erg)["head"].get("X-Rows-Tag", "")
        for _ in range(3):
            if info(erg, sid)["state"] != "held-by-cli":
                return "cost: holder lost"
            rows(erg, tag)
    finally:
        count.done()
        undo()
    if count.n > 1:
        return "cost: 7 unchanged polls made %d full /proc passes (want at most 1)" % count.n
    return None


def check_grok_starts_and_quits(erg):
    sid = new_sid(2)
    failure = expect(erg, sid, "stopped")
    if failure:
        return failure
    proc = popen([GROK, "-c", FAKE, "-r", sid])
    wait_for(lambda: grok_argv(proc.pid))
    failure = expect(erg, sid, "held-by-cli")
    if failure:
        return "start: " + failure
    proc.kill()
    proc.wait()
    failure = expect(erg, sid, "stopped")
    return "quit: " + failure if failure else None


def grok_argv(pid):
    return cmdline(pid).startswith(GROK.encode() + b"\0")


def check_wrapper_execs_grok(erg):
    sid = new_sid(3)
    go = os.path.join(TMP, "go3")
    proc = popen(["sh", "-c", 'while [ ! -e "$0" ]; do sleep 0.02; done; exec "$1" -c "$2" -r "$3"',
                  go, GROK, FAKE, sid])
    wait_for(lambda: cmdline(proc.pid).startswith(b"sh\0"))
    # A fresh snapshot now holds the wrapper as a plain sh: its later exec
    # must still be seen.
    getattr(erg, "_POLL_SCAN", {}).clear()
    failure = expect(erg, sid, "stopped")
    if failure:
        return "before exec: " + failure
    open(go, "w").close()
    if not wait_for(lambda: grok_argv(proc.pid)):
        return "wrapper never exec'd grok"
    failure = expect(erg, sid, "held-by-cli")
    proc.kill()
    proc.wait()
    return "after exec: " + failure if failure else None


OPENS = ("import os,sys,time\n"
         "flag,path=sys.argv[1],sys.argv[2]; held=None\n"
         "while True:\n"
         "    if held is None and os.path.exists(flag): held=open(path,'a')\n"
         "    time.sleep(0.02)\n")


def check_grok_opens_session_later(erg):
    # A grok with no id on its command line opens the session's file after a poll.
    sid = new_sid(12)
    flag = os.path.join(TMP, "open12")
    events = os.path.join(HOME, ".grok", "sessions", CWD.replace("/", "%2F"), sid, "events.jsonl")
    proc = popen([GROK, "-c", OPENS, flag, events])
    wait_for(lambda: grok_argv(proc.pid))
    failure = expect(erg, sid, "stopped")
    if failure:
        return "open later: " + failure
    open(flag, "w").close()
    opened = lambda: any(os.readlink("/proc/%d/fd/%s" % (proc.pid, fd)) == events
                         for fd in os.listdir("/proc/%d/fd" % proc.pid))
    if not wait_for(opened):
        return "open later: grok never opened the file"
    got = info(erg, sid)
    proc.kill()
    proc.wait()
    holder = got["holder"]
    if not holder or holder["pid"] != proc.pid:
        return "open later: poll says holder=%s, want pid %d" % (holder, proc.pid)
    return None


def check_pid_reused(erg):
    # The holder's pid now runs another program (simulated: same pid, new argv).
    sid = new_sid(4)
    proc = popen([GROK, "-c", FAKE, "-r", sid])
    wait_for(lambda: grok_argv(proc.pid))
    failure = expect(erg, sid, "held-by-cli")
    if failure:
        return "reuse setup: " + failure
    real = erg.read_proc_cmdline

    def reused(pid):
        return b"sleep\x00600\x00" if int(pid) == proc.pid else real(pid)
    erg.read_proc_cmdline = reused
    try:
        failure = expect(erg, sid, "stopped")
    finally:
        erg.read_proc_cmdline = real
        proc.kill()
        proc.wait()
    return "reuse: " + failure if failure else None


def reuse_pid(erg, pid, ppid):
    """Make pid read as a new process with the same argv: new start time, given parent."""
    real_ident = getattr(erg, "_proc_ident", None)
    real_ppid = erg.proc_stat_ppid
    if real_ident is not None:
        erg._proc_ident = lambda p: (str(ppid).encode(), b"999999999") if int(p) == pid else real_ident(p)
    erg.proc_stat_ppid = lambda p: ppid if int(p) == pid else real_ppid(p)

    def undo():
        if real_ident is not None:
            erg._proc_ident = real_ident
        erg.proc_stat_ppid = real_ppid
    return undo


def check_pid_reused_same_argv(erg):
    # This server's wrapper runs grok; that grok exits and an outside grok with
    # the same argv gets its pid under another parent: it must read as a holder.
    sid = new_sid(13)
    wrapper = ["sh", "-c", '"$0" -c "$1" --resume "$2"; sleep 600', GROK, FAKE, sid]
    live, _reused = erg.open_live(sid, wrapper, CWD, 80, 24)
    child = []
    wait_for(lambda: child.extend(p for p in (int(n) for n in os.listdir("/proc") if n.isdigit())
                                  if grok_argv(p) and sid.encode() in cmdline(p)) or child)
    if not child:
        return "same argv: grok under the wrapper never started"
    got = info(erg, sid)
    if not got["here"] or got["holder"]:
        erg.kill_proc(live.proc)
        return "same argv setup: here=%s holder=%s" % (got["here"], got["holder"])
    undo = reuse_pid(erg, child[0], 1)
    try:
        got = info(erg, sid)
    finally:
        undo()
        erg.kill_proc(live.proc)
        with erg.LIVE_LOCK:
            erg.LIVE.pop(sid, None)
        try:
            os.kill(child[0], signal.SIGKILL)
        except OSError:
            pass
    if not got["holder"] or got["holder"]["pid"] != child[0]:
        return "same argv: poll says holder=%s, want pid %d" % (got["holder"], child[0])
    return None


def check_new_start_time_rescans(erg):
    # Same pid, argv and parent, new start time: another process. Rescan.
    sid = new_sid(14)
    proc = popen([GROK, "-c", FAKE, "-r", sid])
    wait_for(lambda: grok_argv(proc.pid))
    failure = expect(erg, sid, "held-by-cli")
    if failure:
        return "start time setup: " + failure
    undo = reuse_pid(erg, proc.pid, os.getpid())
    count = Count(erg)
    try:
        info(erg, sid)
    finally:
        count.done()
        undo()
        proc.kill()
        proc.wait()
    return None if count.n == 1 else "start time: %d full /proc passes after a new start time (want 1)" % count.n


def check_turn_start_and_end(erg):
    sid = new_sid(5)
    flag = os.path.join(TMP, "turn5")
    proc = popen([GROK, "-c", TURN, flag, "-r", sid])
    wait_for(lambda: grok_argv(proc.pid))
    if info(erg, sid)["turn"] != "idle":
        return "turn: not idle at start"
    open(flag, "w").close()
    marked = lambda: any(b"agent turn in progress" in cmdline(int(n)).replace(b"\0", b" ")
                         and erg.proc_stat_ppid(int(n)) == proc.pid
                         for n in os.listdir("/proc") if n.isdigit())
    if not wait_for(marked):
        return "turn: mark child never started"
    got = info(erg, sid)["turn"]
    if got != "working":
        return "turn start: poll says %s, want working" % got
    os.unlink(flag)
    if not wait_for(lambda: not marked()):
        return "turn: mark child never ended"
    got = info(erg, sid)["turn"]
    proc.kill()
    proc.wait()
    return None if got == "idle" else "turn end: poll says %s, want idle" % got


def check_own_start_and_stop(erg):
    sid = new_sid(6)
    failure = expect(erg, sid, "stopped")
    if failure:
        return failure
    live, _reused = erg.open_live(sid, [GROK, "-c", FAKE, "--resume", sid], CWD, 80, 24)
    got = info(erg, sid)
    if not got["here"] or got["holder"]:
        return "own start: poll says here=%s holder=%s" % (got["here"], got["holder"])
    erg.kill_proc(live.proc)
    with erg.LIVE_LOCK:
        erg.LIVE.pop(sid, None)
    failure = expect(erg, sid, "stopped")
    return "own stop: " + failure if failure else None


def check_reparent(erg):
    # This server's wrapper runs grok as a child; the wrapper dies, grok is
    # reparented away and becomes a holder.
    sid = new_sid(7)
    wrapper = ["sh", "-c", '"$0" -c "$1" --resume "$2"; :', GROK, FAKE, sid]
    live, _reused = erg.open_live(sid, wrapper, CWD, 80, 24)
    child = []
    wait_for(lambda: child.extend(p for p in (int(n) for n in os.listdir("/proc") if n.isdigit())
                                  if grok_argv(p) and sid.encode() in cmdline(p)) or child)
    if not child:
        return "reparent: grok under the wrapper never started"
    got = info(erg, sid)
    if not got["here"] or got["holder"]:
        return "reparent setup: here=%s holder=%s" % (got["here"], got["holder"])
    os.kill(live.proc.pid, signal.SIGKILL)
    live.proc.wait()
    got = info(erg, sid)
    try:
        os.kill(child[0], signal.SIGKILL)
    except OSError:
        pass
    with erg.LIVE_LOCK:
        erg.LIVE.pop(sid, None)
    if not got["holder"] or got["holder"]["pid"] != child[0]:
        return "reparent: poll says holder=%s, want pid %d" % (got["holder"], child[0])
    return None


def check_concurrent_polls_agree(erg):
    sid = new_sid(8)
    proc = popen([GROK, "-c", FAKE, "-r", sid])
    wait_for(lambda: grok_argv(proc.pid))
    seen, errors = [], []

    def tab():
        try:
            for _ in range(10):
                seen.append(info(erg, sid)["state"])
                rows(erg)
        except Exception as exc:
            errors.append(repr(exc))
    threads = [threading.Thread(target=tab) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    proc.kill()
    proc.wait()
    if errors or set(seen) != {"held-by-cli"}:
        return "concurrent: states %s errors %s" % (sorted(set(seen)), errors[:2])
    return None


def check_slow_scan_holds_no_lock(erg):
    sid = new_sid(9)
    real = erg.ProcScan
    inside = threading.Event()

    class Slow(real):
        def __init__(self):
            inside.set()
            time.sleep(1.5)
            super().__init__()
    poll_lock = getattr(erg, "_POLL_SCAN_LOCK", threading.Lock())
    with poll_lock:
        getattr(erg, "_POLL_SCAN", {}).clear()
    erg.ProcScan = Slow
    poll = threading.Thread(target=info, args=(erg, sid))
    poll.start()
    try:
        inside.wait(5)
        erg.ProcScan = real
        start = time.monotonic()
        with erg.LIVE_LOCK:
            pass
        with poll_lock:
            pass
        took = time.monotonic() - start
        erg.session_state(sid)
    finally:
        erg.ProcScan = real
        poll.join(10)
    return None if took < 1.0 else "slow scan: action waited %.2f s" % took


def check_listing_fails(erg):
    sid = new_sid(10)
    info(erg, sid)
    real = os.listdir
    calls = []

    def fail_once(path="."):
        if path == "/proc" and not calls:
            calls.append(path)
            raise OSError("listing failed")
        return real(path)
    os.listdir = fail_once
    try:
        got = info(erg, sid)
    finally:
        os.listdir = real
    proc = popen([GROK, "-c", FAKE, "-r", sid])
    wait_for(lambda: grok_argv(proc.pid))
    after = info(erg, sid)["state"]
    proc.kill()
    proc.wait()
    if got.get("state") != "stopped" or after != "held-by-cli":
        return "listing failed: then %s, next %s" % (got.get("state"), after)
    return None


def check_actions_scan_fresh(erg):
    sid = new_sid(11)
    info(erg, sid)
    count = Count(erg)
    try:
        erg.session_state(sid)
        erg.session_holder(sid)
        erg.settled_state(sid, with_turn=False)
    finally:
        count.done()
    return None if count.n == 3 else "actions: %d fresh scans for 3 lookups" % count.n


def main():
    erg = load()
    erg.print = lambda *a, **k: None
    checks = (check_unchanged_polls_reuse_one_pass, check_grok_starts_and_quits,
              check_wrapper_execs_grok, check_grok_opens_session_later, check_pid_reused,
              check_pid_reused_same_argv, check_new_start_time_rescans, check_turn_start_and_end,
              check_own_start_and_stop, check_reparent, check_concurrent_polls_agree,
              check_slow_scan_holds_no_lock, check_listing_fails, check_actions_scan_fresh)
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
        for name in os.listdir("/proc"):
            if name.isdigit() and cmdline(int(name)).startswith(GROK.encode() + b"\0"):
                try:
                    os.kill(int(name), signal.SIGKILL)
                except OSError:
                    pass
        shutil.rmtree(TMP, ignore_errors=True)
    for failure in failures:
        print("FAIL: " + failure)
    if failures:
        return 1
    print("PASS: polls reuse one /proc pass and still see every change")
    return 0


if __name__ == "__main__":
    sys.exit(main())
