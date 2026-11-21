"""A/B check for issue #41: a mid-turn session reads working with no systemd-inhibit child.

  python3 -B turn_events.py run <code-dir> <port> <work-dir>  -> JSON checks on stdout
  python3 -B turn_events.py summary <A-json> <B-json>          -> table on stdout;
                                                                exit 1 when any check fails on B

The stand-in grok (fakegrok.py) never starts systemd-inhibit, as on a host
without it.  The run writes grok's turn_started and turn_ended lines to the
session's events.jsonl and reads the chat status poll (/session-info?id=)
and the list poll (/list-rows).  Stdlib only.
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ORDER = ["turn_idle_before", "turn_working", "list_working", "turn_idle_after", "list_idle_after"]
# Long enough for a loaded host, short enough that A's misses end soon.
SETTLE = 8.0


def request(base, path, body=None):
    headers = {"Origin": base, "Content-Type": "application/json"}
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except OSError as exc:
        return None, type(exc).__name__


def check(ok, value):
    return {"ok": bool(ok), "value": str(value)[:60]}


def wait_for(fn, deadline):
    end = time.monotonic() + deadline
    while True:
        got = fn()
        if got[0] or time.monotonic() >= end:
            return got
        time.sleep(0.1)


def event(kind):
    now = datetime.now(timezone.utc)
    obj = {"ts": now.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (now.microsecond // 1000), "type": kind}
    if kind == "turn_ended":
        obj["outcome"] = "completed"
    return json.dumps(obj, separators=(",", ":")) + "\n"


def run(code, port, work):
    home, bin_dir, cwd = os.path.join(work, "home"), os.path.join(work, "bin"), os.path.join(work, "work")
    sid = str(uuid.uuid4())
    folder = os.path.join(home, ".grok", "sessions", "ab", sid)
    for path in (folder, bin_dir, cwd):
        os.makedirs(path)
    with open(os.path.join(folder, "summary.json"), "w") as handle:
        json.dump({"info": {"id": sid, "cwd": cwd}, "generated_title": "AB turn session",
                   "updated_at": "2026-01-01T00:00:00Z"}, handle)
    events = os.path.join(folder, "events.jsonl")
    with open(os.path.join(bin_dir, "grok"), "w") as handle:
        handle.write('#!/bin/bash\nexec -a grok python3 -B "%s/fakegrok.py" "$@"\n' % HERE)
    os.chmod(os.path.join(bin_dir, "grok"), 0o755)
    config = os.path.join(work, "config.json")
    with open(config, "w") as handle:
        json.dump({"bind": "127.0.0.1", "port": int(port)}, handle)
    env = dict(os.environ, HOME=home, ERGOSPHERE_CONFIG=config, PATH=bin_dir + ":" + os.environ["PATH"])
    log = open(os.path.join(work, "server.log"), "w")
    server = subprocess.Popen(
        [sys.executable, "-B", os.path.join(code, "ergosphere.py"), "--bind", "127.0.0.1", "--port", str(port)],
        cwd=work, env=env, stdout=log, stderr=subprocess.STDOUT)
    base = "http://127.0.0.1:%s" % port
    checks = {}

    def info():
        status, text = request(base, "/session-info?id=" + sid)
        try:
            return json.loads(text) if status == 200 else {}
        except ValueError:
            return {}

    def turn_is(want):
        data = info()
        return data.get("turn") == want, "%s turn=%s" % (data.get("state"), data.get("turn"))

    def working_count(want):
        status, text = request(base, "/list-rows")
        mark = 'id="count-working">Working now: '
        cut = text.find(mark) if status == 200 else -1
        count = text[cut + len(mark):].split("<", 1)[0] if cut >= 0 else "?"
        return count == str(want), "%s Working now: %s" % (status, count)

    try:
        ready = lambda: ("ERGOSPHERE_READY" in open(os.path.join(work, "server.log")).read(), "")
        if not wait_for(ready, 60)[0]:
            raise RuntimeError("server not ready")
        request(base, "/resume", {"id": sid})
        if not wait_for(lambda: (str(info().get("state", "")).startswith("running"), ""), 60)[0]:
            raise RuntimeError("session did not start")
        with open(events, "a") as handle:
            handle.write(event("loop_started"))
        checks["turn_idle_before"] = check(*turn_is("idle"))
        with open(events, "a") as handle:
            handle.write(event("turn_started") + event("phase_changed"))
        checks["turn_working"] = check(*wait_for(lambda: turn_is("working"), SETTLE))
        checks["list_working"] = check(*wait_for(lambda: working_count(1), SETTLE))
        with open(events, "a") as handle:
            handle.write(event("turn_ended"))
        checks["turn_idle_after"] = check(*wait_for(lambda: turn_is("idle"), SETTLE))
        checks["list_idle_after"] = check(*wait_for(lambda: working_count(0), SETTLE))
    except (RuntimeError, OSError) as exc:
        for name in ORDER:
            checks.setdefault(name, check(False, "not reached: %s" % exc))
    finally:
        server.terminate()
        try:
            server.wait(10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()
        log.close()
    return {"checks": checks}


def summary(apath, bpath):
    load = lambda p: (json.load(open(p)).get("checks") or {}) if os.path.exists(p) else {}
    a, b = load(apath), load(bpath)
    width = 36
    lines = ["", "Issue #41: a turn open in events.jsonl, no systemd-inhibit child (stand-in grok)",
             "%-18s %-*s %-*s %s" % ("check", width, "A", width, "B", "same")]
    regress, broken = [], []
    for name in ORDER:
        ca = a.get(name) or {"ok": False, "value": "not run"}
        cb = b.get(name) or {"ok": False, "value": "not run"}
        cell = lambda c: ("%s %s" % ("PASS" if c["ok"] else "FAIL", c["value"]))[:width]
        if ca["ok"] and not cb["ok"]:
            regress.append(name)
        if not cb["ok"]:
            broken.append(name)
        lines.append("%-18s %-*s %-*s %s" % (name, width, cell(ca), width, cell(cb), "yes" if ca == cb else "NO"))
    if regress:
        lines.append("REGRESSION: passes on A, fails on B: %s" % ", ".join(regress))
    if broken:
        lines.append("FAIL on B: %s" % ", ".join(broken))
    print("\n".join(lines))
    return 1 if broken else 0


def main(argv):
    if len(argv) == 5 and argv[1] == "run":
        print(json.dumps(run(argv[2], argv[3], argv[4])))
        return 0
    if len(argv) == 4 and argv[1] == "summary":
        return summary(argv[2], argv[3])
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
