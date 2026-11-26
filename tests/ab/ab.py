"""HTTP-level A/B checks and the side-by-side summary.

  python3 -B ab.py http <port> <session-id> [<folder-gone-id>]  -> JSON checks on stdout
  python3 -B ab.py vendor <code-dir>          -> JSON vendor-manifest check on stdout
  python3 -B ab.py builtin                    -> JSON list of built-in check names (ORDER and HTTP_CHECKS),
                                                 which browser.d/ modules may not reuse
  python3 -B ab.py summary <A-dir> <B-dir>    -> summary text on stdout;
                                                 exit 1 when a check passes on A and fails on B,
                                                 or B's browser checks break or miss a check in ORDER or browser.d/
  python3 -B ab.py pidreuse <A.txt> <B.txt>   -> pid reuse rows (tests/pid_reuse_test.py output);
                                                 exit 1 unless B ran and passed every check
Stdlib only.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

TITLE = "AB seeded session"
ORDER = ["vendor_listed", "health", "session_list", "open_session", "folder_missing", "terminal", "wheel_scroll", "bar_drag", "touch_scroll", "resize", "start_size", "keyboard_android", "keyboard_ios", "resume_box", "ended_view", "settings_poll"]
# Checks that do not come from browser.js (HTTP and the vendor manifest).
HTTP_CHECKS = ("vendor_listed", "health", "session_list", "open_session", "folder_missing")


def module_names():
    """Page checks from browser.d/ modules (#243): one per <name>.js, in this
    tree and, when AB_A_DIR is set, the base tree.  Both sides run this tree's
    browser.js, so a module deleted here is missing on both; the base tree
    still names it, so it is still required."""
    dirs = [Path(__file__).resolve().parent / "browser.d"]
    if os.environ.get("AB_A_DIR"):
        dirs.append(Path(os.environ["AB_A_DIR"]) / "tests" / "ab" / "browser.d")
    return sorted({p.stem for d in dirs for p in d.glob("*.js")} - set(ORDER))


def all_checks():
    """ORDER with the browser.d/ checks before resume_box, which runs after them."""
    i = ORDER.index("resume_box") if "resume_box" in ORDER else len(ORDER)
    return ORDER[:i] + module_names() + ORDER[i:]


def get(base, path):
    try:
        with urllib.request.urlopen(base + path, timeout=60) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def post(base, path, body):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(), headers={
        "Origin": base, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def check(ok, value):
    return {"ok": bool(ok), "value": str(value)[:60]}


def http_checks(port, sid, gone=""):
    base = "http://127.0.0.1:%s" % port
    checks, info = {}, {}
    try:
        code, text = get(base, "/health")
        data = json.loads(text) if code == 200 else {}
        info["code_sha256"] = str(data.get("code_sha256") or "")[:12]
        checks["health"] = check(data.get("ok") is True and data.get("product") == "ergosphere",
                                 "%s ok=%s" % (code, data.get("ok")))
        code, text = get(base, "/")
        checks["session_list"] = check(code == 200 and TITLE in text,
                                       "%s %s" % (code, "listed" if TITLE in text else "not listed"))
        code, text = post(base, "/resume", {"id": sid})
        state = ""
        if code == 200:
            end = time.monotonic() + 60
            while time.monotonic() < end:
                c2, t2 = get(base, "/session-info?id=" + sid)
                state = json.loads(t2).get("state", "") if c2 == 200 else ""
                if state.startswith("running"):
                    break
                time.sleep(0.2)
        checks["open_session"] = check(code == 200 and state.startswith("running"),
                                       "resume %s state=%s" % (code, state or "-"))
        if gone:
            # Issue #43: the state, /resume, the list row and the page all say so.
            c1, t1 = get(base, "/session-info?id=" + gone)
            gstate = json.loads(t1).get("state", "") if c1 == 200 else ""
            c2, t2 = post(base, "/resume", {"id": gone})
            rstate = json.loads(t2).get("state", "") if t2.startswith("{") else ""
            _, page = get(base, "/")
            marked = "AB folder gone [folder missing]" in page
            _, chat = get(base, "/chat?resume=" + gone)
            says = '<span id="status">folder missing</span>' in chat
            checks["folder_missing"] = check(
                gstate == "folder-missing" and c2 == 409 and rstate == "folder-missing" and marked and says,
                "state=%s resume=%s list=%s page=%s" % (gstate or "-", c2, "y" if marked else "n", "y" if says else "n"))
    except (OSError, ValueError) as exc:
        checks.setdefault("health", check(False, "error %s" % exc))
        for name in HTTP_CHECKS:
            checks.setdefault(name, check(False, "not reached"))
    return {"checks": checks, "info": info}


def vendor_checks(code_dir):
    """vendor/ files match the versions package-lock.json lists, so scanners see them (#7)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import vendor_manifest_test
    results = list(vendor_manifest_test.check(Path(code_dir)))
    bad = [msg for ok, msg in results if not ok]
    return {"checks": {"vendor_listed": check(not bad, bad[0] if bad else "%d checks" % len(results))}}


def load(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def side(dirname):
    server = load(dirname + "/http.json")
    browser = load(dirname + "/browser.json")
    checks = dict(server.get("checks") or {})
    checks.update(load(dirname + "/vendor.json").get("checks") or {})
    bchecks = dict(browser.get("checks") or {})
    # A browser result under an HTTP check's name would replace the real one
    # and hide a regression (#243): drop it and mark the side broken.
    reused = sorted(n for n in bchecks if n in HTTP_CHECKS)
    for n in reused:
        del bchecks[n]
    checks.update(bchecks)
    if reused:
        browser = dict(browser, reused=reused)
    return checks, server.get("info") or {}, browser


# B is slow when its median wheel round trip is over both the floor and
# SLOW_RATIO times A's, measured in the same run on the same host (#151).
# A median of several samples, never one reading: one slow sample under load
# does not move it, and the ratio absorbs a host that slows both sides.
SLOW_FLOOR_MS = 2000
SLOW_RATIO = 3


def median(xs):
    xs = sorted(x for x in xs if isinstance(x, (int, float)) and not isinstance(x, bool))
    return xs[len(xs) // 2] if xs else None


def latency(a, b):
    """Takes the wheel round trips that browser.d/input_latency.js puts in its
    check (so they do not make A and B differ) and marks B's input_latency
    failed when B is much slower than A, so the table reports a regression."""
    taken = []
    for side_checks in (a, b):
        c = side_checks.get("input_latency")
        xs = c.pop("samples", None) if isinstance(c, dict) else None
        taken.append(median(xs) if isinstance(xs, list) else None)
    ma, mb = taken
    if ma is None or mb is None:
        return []
    limit = max(SLOW_FLOOR_MS, SLOW_RATIO * ma)
    if mb > limit and b["input_latency"].get("ok"):
        b["input_latency"] = check(False, "slow: median %sms, A %sms" % (mb, ma))
    return ["input_latency median ms: A=%s B=%s (B fails over %s)" % (ma, mb, limit)]


def summary(adir, bdir):
    a, ainfo, abrowser = side(adir)
    b, binfo, bbrowser = side(bdir)
    lines = ["Ergosphere A/B: A = base, B = PR combined with base, stand-in grok TUI, throwaway HOME"]
    lines.append("code_sha256 A=%s B=%s" % (ainfo.get("code_sha256") or "-", binfo.get("code_sha256") or "-"))
    if abrowser.get("browser") and bbrowser.get("browser"):
        lines.append("browser: headless Chromium %s ran the page checks" % abrowser["browser"])
    else:
        why = lambda r: str(r.get("browser") and "ran" or r.get("unavailable") or r.get("error") or "no result")
        lines.append("BROWSER CHECKS DID NOT RUN (A: %s; B: %s)" % (why(abrowser)[:150], why(bbrowser)[:150]))
    lines.extend(latency(a, b))
    # No browser on this host is a warning; a browser.js that breaks on B, or
    # that ran but left out a page check in ORDER, fails the run.  Both sides
    # run B's browser.js, so a check dropped there is missing on A too:
    # comparing A's count with B's cannot catch it.
    broken = []
    if not bbrowser.get("browser") and not bbrowser.get("unavailable"):
        broken.append("browser checks broke on B: %s" % str(bbrowser.get("error") or "no result")[:300])
    for label, br in (("A", abrowser), ("B", bbrowser)):
        if br.get("reused"):
            broken.append("browser checks on %s reused HTTP check name(s): %s" % (label, ", ".join(br["reused"])))
    names = all_checks()
    if bbrowser.get("browser"):
        missing = [n for n in names if n not in HTTP_CHECKS and n not in (bbrowser.get("checks") or {})]
        if missing:
            broken.append("B did not report browser check(s): %s" % ", ".join(missing))
    width = 36
    lines.append("")
    lines.append("%-14s %-*s %-*s %s" % ("check", width, "A", width, "B", "same"))
    differ, regress = [], []
    for name in names:
        if name not in a and name not in b:
            continue
        ca = a.get(name) or {"ok": False, "value": "not run"}
        cb = b.get(name) or {"ok": False, "value": "not run"}
        cell = lambda c: ("%s %s" % ("PASS" if c["ok"] else "FAIL", c["value"]))[:width]
        same = ca == cb
        if not same:
            differ.append(name)
        if ca["ok"] and not cb["ok"]:
            regress.append(name)
        lines.append("%-14s %-*s %-*s %s" % (name, width, cell(ca), width, cell(cb), "yes" if same else "NO"))
    lines.append("")
    if not differ:
        lines.append("result: A and B are the same on every check")
    else:
        lines.append("result: %d check(s) differ: %s" % (len(differ), ", ".join(differ)))
    if regress:
        lines.append("REGRESSION: passes on A, fails on B: %s" % ", ".join(regress))
    for why in broken:
        lines.append("BROWSER FAILURE: %s" % why)
    print("\n".join(lines))
    return 1 if regress or broken else 0


# Every check tests/pid_reuse_test.py runs, by name.  B must report each one:
# a crash or skip part-way leaves rows out, and a dropped check would vanish
# from both columns (the #179 rule for HTTP checks).
PID_REUSE_CHECKS = [
    "legit quit (pidfd)",
    "pid reused before the signal",
    "pid reused after the pidfd opened",
    "same-session grok relaunched at the pid, re-read sees it",
    "same-session grok relaunched at the pid after the re-read",
    "same-session grok relaunched, re-read sees it (pidfd_send_signal EPERM by policy)",
    "checked grok exits just before the signal",
    "checked grok quits right after the re-read",
    "legit quit (no pidfd fallback)",
    "pid reused before the signal (no pidfd fallback)",
    "legit quit (pidfd_send_signal ENOSYS)",
    "legit quit (pidfd_send_signal EPERM by policy)",
    "another user's grok (every signal EPERM) refused",
]


def pid_reuse_rows(path):
    """({check name: passed}, exit status or None) from tests/pid_reuse_test.py output.

    Rows are "ok: name" / "FAIL: name -- why"; run.sh appends "exit: N".
    """
    rows, status = {}, None
    for line in open(path, errors="replace"):
        mark, _, rest = line.rstrip("\n").partition(": ")
        if mark == "exit" and rest.strip().lstrip("-").isdigit():
            status = int(rest)
        elif mark in ("ok", "FAIL") and not rest[:1].isdigit():  # not the "FAIL: 3 of 13" total
            rows[rest.split(" -- ")[0]] = mark == "ok"
    return rows, status


def pidreuse(apath, bpath):
    (a, _astatus), (b, bstatus) = pid_reuse_rows(apath), pid_reuse_rows(bpath)
    cell = lambda ok: "-" if ok is None else ("PASS" if ok else "FAIL")
    lines = ["", "Issues #148, #191: quitting a holder grok whose pid is reused (stand-ins, private pid namespace)",
             "%-84s %-4s %s" % ("check", "A", "B")]
    names = PID_REUSE_CHECKS + [n for n in b if n not in PID_REUSE_CHECKS]
    lines += ["%-84s %-4s %s" % (name, cell(a.get(name)), cell(b.get(name))) for name in names]
    bad = []
    missing = [n for n in PID_REUSE_CHECKS if n not in b]
    if missing:
        bad.append("PID REUSE FAILURE: B did not report check(s): %s" % ", ".join(missing))
    failed = [n for n in names if b.get(n) is False]
    if failed:
        bad.append("PID REUSE FAILURE: fails on B: %s" % ", ".join(failed))
    if bstatus != 0:
        tail = open(bpath, errors="replace").read().strip().splitlines()[-3:]
        bad.append("PID REUSE FAILURE: B's test exited %s: %s" % (bstatus, " | ".join(tail)))
    print("\n".join(lines + bad))
    return 1 if bad else 0


def main(argv):
    if len(argv) == 4 and argv[1] == "pidreuse":
        return pidreuse(argv[2], argv[3])
    if len(argv) in (4, 5) and argv[1] == "http":
        print(json.dumps(http_checks(*argv[2:])))
        return 0
    if len(argv) == 3 and argv[1] == "vendor":
        print(json.dumps(vendor_checks(argv[2])))
        return 0
    if len(argv) == 2 and argv[1] == "builtin":
        print(json.dumps(sorted(set(ORDER) | set(HTTP_CHECKS))))
        return 0
    if len(argv) == 4 and argv[1] == "summary":
        return summary(argv[2], argv[3])
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
