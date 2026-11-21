"""Turn state from grok's events.jsonl, with no systemd-inhibit child (#41).

grok writes turn_started and turn_ended to the session's events.jsonl. Where
systemd-inhibit cannot run, grok has no "agent turn in progress" child, and
the session must still read working mid-turn.

Covers: a turn open in events.jsonl reads working, for this server's grok and
for a holder; turn_ended makes it idle; a turn_started older than the live
grok (left by a grok killed mid-turn) reads idle; a line still being written
is read once complete; a replaced (shorter) file is read from the start; the
inhibit child alone still reads working; concurrent polls agree with the last
event written.

Run: python3 -B tests/turn_events_test.py
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
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = tempfile.mkdtemp(prefix="ergo-turn-")
HOME = os.path.join(TMP, "home")
BIN = os.path.join(TMP, "bin")
GROK = os.path.join(BIN, "grok")
CWD = os.path.join(HOME, "proj")
os.makedirs(BIN)
os.makedirs(CWD)
os.symlink(sys.executable, GROK)
os.environ["HOME"] = HOME
# Prints once (so this server's entry reads running), then sleeps.
FAKE = "import sys,time\nsys.stdout.write('ready\\n'); sys.stdout.flush()\nwhile True: time.sleep(1)\n"
# Same, plus the child real grok runs during a turn where systemd-inhibit exists.
PROCS = []
RUN = str(uuid.uuid4())[9:]
# The run id in the child's argv lets cleanup find it and nothing else.
MARKED = ("import subprocess,time\n"
          "subprocess.Popen(['sh','-c','sleep 600','agent turn '+'in progress %s'])\n"
          "while True: time.sleep(1)\n" % RUN)


def load():
    spec = importlib.util.spec_from_file_location("ergosphere", ROOT / "ergosphere.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def new_sid(n):
    # Ids differ per run: the holder scan sees every grok on the host.
    sid = "%08d-%s" % (n, RUN)
    folder = os.path.join(HOME, ".grok", "sessions", CWD.replace("/", "%2F"), sid)
    os.makedirs(folder)
    with open(os.path.join(folder, "summary.json"), "w") as fh:
        json.dump({"id": sid, "cwd": CWD, "title": "t%d" % n}, fh)
    return sid


def events(sid):
    return os.path.join(HOME, ".grok", "sessions", CWD.replace("/", "%2F"), sid, "events.jsonl")


def stamp(at=None):
    moment = datetime.fromtimestamp(time.time() if at is None else at, timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (moment.microsecond // 1000)


def line(kind, at=None):
    obj = {"ts": stamp(at), "type": kind}
    if kind == "turn_ended":
        obj["outcome"] = "completed"
    return json.dumps(obj, separators=(",", ":")) + "\n"


def append(sid, text):
    with open(events(sid), "a") as fh:
        fh.write(text)


def holder(sid, code=FAKE):
    proc = subprocess.Popen([GROK, "-c", code, "--resume", sid], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    PROCS.append(proc)
    return proc


def wait_for(fn, secs=10):
    end = time.time() + secs
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.05)
    return False


def turn(erg, sid):
    return erg.session_state(sid).get("turn")


def check_holder_mid_turn(erg):
    sid = new_sid(1)
    append(sid, line("loop_started"))
    proc = holder(sid)
    if not wait_for(lambda: erg.session_state(sid)["state"] != "stopped"):
        return "holder: grok never seen"
    if turn(erg, sid) != "idle":
        return "holder: no turn yet, want idle, got %s" % turn(erg, sid)
    append(sid, line("turn_started") + line("phase_changed"))
    if turn(erg, sid) != "working":
        return "holder: turn_started with no inhibit child, want working, got %s" % turn(erg, sid)
    append(sid, line("turn_ended"))
    if turn(erg, sid) != "idle":
        return "holder: after turn_ended, want idle, got %s" % turn(erg, sid)
    proc.kill()
    return None


def check_own_mid_turn(erg):
    sid = new_sid(2)
    live, _ = erg.open_live(sid, [GROK, "-c", FAKE, "--resume", sid], CWD, 80, 24)
    if not wait_for(lambda: erg.session_state(sid)["state"].startswith("running")):
        return "own: grok never ran (%s)" % erg.session_state(sid)["state"]
    append(sid, line("turn_started"))
    state = erg.session_state(sid)
    if state["state"] != "running-working" or state["turn"] != "working":
        return "own: turn_started, want running-working, got %s" % state["state"]
    append(sid, line("turn_ended"))
    if erg.session_state(sid)["state"] != "running-idle":
        return "own: after turn_ended, want running-idle, got %s" % erg.session_state(sid)["state"]
    erg.kill_proc(live.proc)
    return None


def check_stale_turn(erg):
    # A grok killed mid-turn left turn_started; the grok now on the session started later.
    sid = new_sid(3)
    append(sid, line("turn_started", time.time() - 120))
    proc = holder(sid)
    if not wait_for(lambda: erg.session_state(sid)["state"] != "stopped"):
        return "stale: grok never seen"
    if turn(erg, sid) != "idle":
        return "stale: turn_started from before this grok started, want idle, got %s" % turn(erg, sid)
    append(sid, line("turn_started"))
    if turn(erg, sid) != "working":
        return "stale: a new turn_started, want working, got %s" % turn(erg, sid)
    proc.kill()
    return None


def check_partial_line(erg):
    sid = new_sid(4)
    append(sid, line("turn_started"))
    proc = holder(sid)
    if not wait_for(lambda: erg.session_state(sid)["state"] != "stopped"):
        return "partial: grok never seen"
    if turn(erg, sid) != "working":
        return "partial: want working, got %s" % turn(erg, sid)
    end = line("turn_ended")
    append(sid, end[:12])
    if turn(erg, sid) != "working":
        return "partial: half a turn_ended line, want working, got %s" % turn(erg, sid)
    append(sid, end[12:])
    if turn(erg, sid) != "idle":
        return "partial: line completed, want idle, got %s" % turn(erg, sid)
    # Replaced by a shorter file (new inode) holding an open turn.
    tmp = events(sid) + ".new"
    with open(tmp, "w") as fh:
        fh.write(line("turn_started"))
    os.replace(tmp, events(sid))
    if turn(erg, sid) != "working":
        return "partial: file replaced with an open turn, want working, got %s" % turn(erg, sid)
    proc.kill()
    return None


def check_mark_only(erg):
    sid = new_sid(5)
    proc = holder(sid, MARKED)
    if not wait_for(lambda: turn(erg, sid) == "working"):
        return "mark: inhibit child and no events.jsonl, want working, got %s" % turn(erg, sid)
    proc.kill()
    return None


def check_concurrent_polls(erg):
    sid = new_sid(6)
    proc = holder(sid)
    if not wait_for(lambda: erg.session_state(sid)["state"] != "stopped"):
        return "concurrent: grok never seen"
    stop = threading.Event()
    errors = []

    def poll():
        while not stop.is_set():
            try:
                erg.session_state(sid)
            except Exception as exc:
                errors.append(repr(exc))
                return

    threads = [threading.Thread(target=poll) for _ in range(6)]
    for t in threads:
        t.start()
    for i in range(200):
        append(sid, line("turn_started") + line("phase_changed") * 3)
        if i % 2:
            append(sid, line("turn_ended"))
    stop.set()
    for t in threads:
        t.join()
    if errors:
        return "concurrent: poll raised %s" % errors[0]
    if turn(erg, sid) != "idle":
        return "concurrent: last event turn_ended, want idle, got %s" % turn(erg, sid)
    append(sid, line("turn_started"))
    if turn(erg, sid) != "working":
        return "concurrent: then turn_started, want working, got %s" % turn(erg, sid)
    proc.kill()
    return None


def main():
    erg = load()
    erg.print = lambda *a, **k: None
    checks = (check_holder_mid_turn, check_own_mid_turn, check_stale_turn,
              check_partial_line, check_mark_only, check_concurrent_polls)
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
                proc.wait(5)
            except (OSError, subprocess.TimeoutExpired):
                pass
        # The inhibit stand-in child outlives its killed fake grok.
        for name in os.listdir("/proc"):
            try:
                raw = open("/proc/%s/cmdline" % name, "rb").read() if name.isdigit() else b""
                if RUN.encode() in raw and b"agent turn in progress" in raw:
                    os.kill(int(name), signal.SIGKILL)
            except OSError:
                pass
        shutil.rmtree(TMP, ignore_errors=True)
    for failure in failures:
        print("FAIL: " + failure)
    if failures:
        return 1
    print("PASS: turn state from events.jsonl without systemd-inhibit (#41)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
