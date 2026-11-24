"""A list of many stalled folders cannot exhaust the thread budget (#43 review).

Each folder check runs on its own thread, and a hung mount keeps that thread
forever. A list of hundreds of saved sessions on distinct folders under a hung
mount used to start one thread per folder. Once the user's thread limit was
reached, Thread.start raised RuntimeError out of the list render. Checks are
now capped at FOLDER_CHECK_THREADS; a folder with no slot in time reads as
present, the same as a slow folder.

Run: python3 -B tests/folder_check_thread_cap_test.py
Stdlib only. The hung stat is simulated by blocking os.path.isdir. The thread
limit is a real RLIMIT_NPROC set on a child process, with HEADROOM threads of
room, so 600 stalled folders cannot all get a thread.
"""
import importlib.util
import os
import resource
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STALL_PREFIX = "/mnt/ergo-thread-cap-test/"
STALLED_FOLDERS = 600
HEADROOM = 100
MAX_THREADS = 16  # the cap this test pins; keep in step with FOLDER_CHECK_THREADS


def load(home):
    os.environ["HOME"] = home
    spec = importlib.util.spec_from_file_location("ergosphere", ROOT / "ergosphere.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.print = lambda *a, **k: None
    return mod


def threads_of_user():
    uid = os.getuid()
    total = 0
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            if os.stat("/proc/%s" % pid).st_uid != uid:
                continue
            total += len(os.listdir("/proc/%s/task" % pid))
        except OSError:
            continue
    return total


def child(home):
    # Runs under the thread limit set by the parent. Prints one RESULT line.
    release = threading.Event()
    real_isdir = os.path.isdir

    def hung(path):
        if str(path).startswith(STALL_PREFIX):
            release.wait()  # a stat stuck on a hung mount
            return False
        return real_isdir(path)

    erg = load(home)
    os.path.isdir = hung
    cwds = [STALL_PREFIX + "p%d" % i for i in range(STALLED_FOLDERS)]
    before = threading.active_count()
    try:
        erg.missing_folders(cwds)
        print("RESULT ok threads_added=%d" % (threading.active_count() - before), flush=True)
    except RuntimeError as exc:
        print("RESULT raised threads_added=%d error=%s" % (threading.active_count() - before, exc),
              flush=True)
    finally:
        release.set()
    return 0


def check_quick_folders_still_read_missing(tmp):
    # Normal folders finish at once, so one deleted folder in a list of 41
    # must still read missing with the cap in place.
    erg = load(os.path.join(tmp, "home"))
    base = os.path.join(tmp, "quick")
    os.makedirs(base)
    cwds = []
    for i in range(40):
        path = os.path.join(base, "d%d" % i)
        os.makedirs(path)
        cwds.append(path)
    gone = os.path.join(base, "gone")
    cwds.append(gone)
    out = erg.missing_folders(cwds)
    fails = []
    if out != {gone}:
        fails.append("a 41-folder list should mark only the deleted folder missing, got %d"
                     % len(out))
    return fails


def check_stalled_list_under_thread_limit(tmp):
    limit = threads_of_user() + HEADROOM

    def set_limit():
        resource.setrlimit(resource.RLIMIT_NPROC, (limit, limit))

    proc = subprocess.run(
        [sys.executable, "-B", str(Path(__file__).resolve()), "--child", os.path.join(tmp, "child")],
        preexec_fn=set_limit, capture_output=True, text=True, timeout=120,
    )
    lines = [line for line in proc.stdout.splitlines() if line.startswith("RESULT ")]
    if not lines:
        return ["child printed no RESULT line: %s" % proc.stderr[-300:]]
    result = lines[-1]
    fails = []
    if " raised " in result:
        fails.append("list render raised under the thread limit: %s" % result)
    added = int(result.split("threads_added=")[1].split()[0])
    if added > MAX_THREADS:
        fails.append("%d stalled folders started %d threads, want at most %d"
                     % (STALLED_FOLDERS, added, MAX_THREADS))
    return fails


def main():
    if len(sys.argv) > 2 and sys.argv[1] == "--child":
        return child(sys.argv[2])
    tmp = tempfile.mkdtemp(prefix="ergo-thread-cap-")
    fails = []
    try:
        fails += check_quick_folders_still_read_missing(tmp)
        fails += check_stalled_list_under_thread_limit(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    for f in fails:
        print("FAIL: " + f)
    if fails:
        return 1
    print("PASS: a list of stalled folders starts at most %d check threads and does not raise"
          % MAX_THREADS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
