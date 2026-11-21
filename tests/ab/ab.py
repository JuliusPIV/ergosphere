"""HTTP-level A/B checks and the side-by-side summary.

  python3 -B ab.py http <port> <session-id>   -> JSON checks on stdout
  python3 -B ab.py vendor <code-dir>          -> JSON vendor-manifest check on stdout
  python3 -B ab.py summary <A-dir> <B-dir>    -> summary text on stdout;
                                                 exit 1 when a check passes on A and fails on B,
                                                 or B misses an HTTP check in ORDER,
                                                 or B's browser checks break or miss a check in ORDER
Stdlib only.
"""
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

TITLE = "AB seeded session"
ORDER = ["vendor_listed", "health", "session_list", "open_session", "terminal", "wheel_scroll", "bar_drag", "touch_scroll", "resize", "keyboard_android", "keyboard_ios", "resume_box"]
# Checks that do not come from browser.js (HTTP and the vendor manifest).
HTTP_CHECKS = ("vendor_listed", "health", "session_list", "open_session")


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


def http_checks(port, sid):
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
    except (OSError, ValueError) as exc:
        checks.setdefault("health", check(False, "error %s" % exc))
        for name in ("session_list", "open_session"):
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
    checks.update(browser.get("checks") or {})
    return checks, server.get("info") or {}, browser


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
    # No browser on this host is a warning; a browser.js that breaks on B, or
    # that ran but left out a page check in ORDER, fails the run.  Both sides
    # run B's browser.js, so a check dropped there is missing on A too:
    # comparing A's count with B's cannot catch it.
    broken = []
    if not bbrowser.get("browser") and not bbrowser.get("unavailable"):
        broken.append("browser checks broke on B: %s" % str(bbrowser.get("error") or "no result")[:300])
    if bbrowser.get("browser"):
        missing = [n for n in ORDER if n not in HTTP_CHECKS and n not in (bbrowser.get("checks") or {})]
        if missing:
            broken.append("B did not report browser check(s): %s" % ", ".join(missing))
    width = 36
    lines.append("")
    lines.append("%-14s %-*s %-*s %s" % ("check", width, "A", width, "B", "same"))
    differ, regress = [], []
    for name in ORDER:
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
    # The HTTP checks always run, and both sides run B's ab.py: a check dropped
    # from http_checks() vanishes from both columns, so require each name (#179).
    http_missing = [n for n in HTTP_CHECKS if n not in (load(bdir + "/http.json").get("checks") or {})]
    if http_missing:
        lines.append("HTTP FAILURE: B did not report HTTP check(s): %s" % ", ".join(http_missing))
    print("\n".join(lines))
    return 1 if regress or broken or http_missing else 0


def main(argv):
    if len(argv) == 4 and argv[1] == "http":
        print(json.dumps(http_checks(argv[2], argv[3])))
        return 0
    if len(argv) == 3 and argv[1] == "vendor":
        print(json.dumps(vendor_checks(argv[2])))
        return 0
    if len(argv) == 4 and argv[1] == "summary":
        return summary(argv[2], argv[3])
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
