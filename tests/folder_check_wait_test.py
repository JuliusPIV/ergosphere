"""A hung folder cannot stall the list page or its poll (#43 review).

folder_missing stats a saved session's folder. On a hung mount that stat never
returns, so GET / and /list-rows must not wait on it. The check is bounded,
a slow folder reads as present, and only one check per folder is outstanding,
so the 4 s poll does not pile up threads.  Many distinct stalled folders in
one list share one wait; they do not add up.

Run: python3 -B tests/folder_check_wait_test.py
Stdlib only. The hung stat is simulated by blocking os.path.isdir.
"""
import importlib.util
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = tempfile.mkdtemp(prefix="ergo-stat-wait-")
os.environ["HOME"] = os.path.join(TMP, "home")
STALLED = "/mnt/ergo-stalled-test/project"
RELEASE = threading.Event()
REAL_ISDIR = os.path.isdir
STALL_CALLS = []


def fake_isdir(path):
    if str(path).startswith(STALLED):
        STALL_CALLS.append(path)
        RELEASE.wait()  # a stat stuck on a hung mount
        return False
    return REAL_ISDIR(path)


def load():
    spec = importlib.util.spec_from_file_location("ergosphere", ROOT / "ergosphere.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def returns_within(fn, seconds=3):
    out = {}

    def run():
        out["value"] = fn()

    th = threading.Thread(target=run, daemon=True)
    th.start()
    th.join(seconds)
    return (not th.is_alive()), out.get("value")


def check_list_rows_tag_bounded(erg):
    rows = [{"id": "s1", "title": "t", "cwd": STALLED, "updated": "x"}]
    done, _ = returns_within(lambda: erg.list_rows_tag(rows, {}))
    return [] if done else ["list_rows_tag blocked on a stalled folder stat"]


def check_dashboard_rows_bounded(erg):
    rows = [{"id": "s1", "title": "t", "cwd": STALLED, "updated": "x"}]
    done, _ = returns_within(lambda: erg.dashboard_rows(rows, {}))
    return [] if done else ["dashboard_rows blocked on a stalled folder stat"]


def check_poll_does_not_pile_up(erg):
    # Across every call in this run (the list checks poll it too) the stuck
    # folder is statted once, while that first stat is still outstanding.
    for _ in range(10):
        returns_within(lambda: erg.folder_missing(STALLED))
    if STALL_CALLS.count(STALLED) != 1:
        return ["stalled folder stat started %d times, want 1" % STALL_CALLS.count(STALLED)]
    return []


def check_many_stalled_folders_share_one_wait(erg):
    # 10 distinct folders on one hung mount: at 0.5 s each that is 5 s, the
    # bound is one wait for the whole render (FOLDER_CHECK_WAIT), checked at 2 s.
    rows = [{"id": "m%d" % i, "title": "t", "cwd": "%s/many-%d" % (STALLED, i), "updated": "x"}
            for i in range(10)]
    start = time.monotonic()
    done, _ = returns_within(lambda: erg.dashboard_rows(rows, {}), seconds=10)
    took = time.monotonic() - start
    if not done or took > 2.0:
        return ["10 stalled folders: one list render took %.1f s, want under 2 s" % took]
    return []


def check_fast_missing_and_present_unchanged(erg):
    fails = []
    gone = os.path.join(TMP, "gone")
    if not erg.folder_missing(gone):
        fails.append("a deleted folder should read missing")
    if erg.folder_missing(TMP):
        fails.append("an existing folder should not read missing")
    if erg.folder_missing(""):
        fails.append("a blank cwd should not read missing")
    return fails


def main():
    erg = load()
    erg.print = lambda *a, **k: None
    os.path.isdir = fake_isdir
    fails = []
    try:
        for check in (check_list_rows_tag_bounded, check_dashboard_rows_bounded,
                      check_poll_does_not_pile_up, check_many_stalled_folders_share_one_wait,
                      check_fast_missing_and_present_unchanged):
            try:
                fails += check(erg)
            except Exception as exc:
                fails.append("%s: %s: %s" % (check.__name__, type(exc).__name__, exc))
    finally:
        RELEASE.set()
        os.path.isdir = REAL_ISDIR
        shutil.rmtree(TMP, ignore_errors=True)
    for f in fails:
        print("FAIL: " + f)
    if fails:
        return 1
    print("PASS: a stalled folder stat does not block the list rows or pile up checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
