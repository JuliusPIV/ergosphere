"""A/B check for issue #45: one session's hung start must not block the others.

  python3 -B slow_start.py run <code-dir> <port> <work-dir>  -> JSON checks on stdout
  python3 -B slow_start.py summary <A-json> <B-json>          -> table on stdout;
                                                               exit 1 when a check passes on A and fails on B
  python3 -B slow_start.py serve <code-dir> <fifo> <server args...>  (used by run)

serve runs that side's ergosphere.py with one change: starting a session
whose folder is named "hung" waits on a fifo first, as a start in a folder on
a stalled network mount waits.  grok is the stand-in TUI (fakegrok.py).
Stdlib only.
"""
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
WAIT = 3.0
ORDER = ["slow_state_read", "slow_reattach", "slow_launch", "slow_second_start", "slow_one_grok"]


def serve(code, fifo, args):
    spec = importlib.util.spec_from_file_location("ergosphere", os.path.join(code, "ergosphere.py"))
    erg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(erg)
    real = erg.spawn_grok

    def spawn(cmd, cwd, cols, rows):
        if os.path.basename(cwd) == "hung":
            open(fifo + ".entered", "w").close()
            with open(fifo, "rb"):
                pass
        return real(cmd, cwd, cols, rows)

    erg.spawn_grok = spawn
    return erg.main(args)


def request(base, path, body=None, timeout=WAIT):
    headers = {"Origin": base, "Content-Type": "application/json"}
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")
    except (OSError, ValueError) as exc:
        return None, {"error": type(exc).__name__}


def check(ok, value):
    return {"ok": bool(ok), "value": str(value)[:60]}


def wait_for(fn, deadline=60):
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        if fn():
            return True
        time.sleep(0.1)
    return False


def seed(home, sid, cwd, title):
    folder = os.path.join(home, ".grok", "sessions", "ab", sid)
    os.makedirs(folder)
    os.makedirs(cwd, exist_ok=True)
    with open(os.path.join(folder, "summary.json"), "w") as handle:
        json.dump({"info": {"id": sid, "cwd": cwd}, "generated_title": title,
                   "updated_at": "2026-01-01T00:00:00Z"}, handle)


def groks_for(sid):
    count = 0
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open("/proc/%s/cmdline" % pid, "rb") as handle:
                parts = handle.read().split(b"\0")
        except OSError:
            continue
        if parts and parts[0] == b"grok" and sid.encode() in parts:
            count += 1
    return count


def run(code, port, work):
    home, bin_dir, fifo = os.path.join(work, "home"), os.path.join(work, "bin"), os.path.join(work, "mount")
    os.makedirs(bin_dir)
    os.mkfifo(fifo)
    slow, other = str(uuid.uuid4()), str(uuid.uuid4())
    seed(home, slow, os.path.join(work, "hung"), "AB slow session")
    seed(home, other, os.path.join(work, "work"), "AB other session")
    with open(os.path.join(bin_dir, "grok"), "w") as handle:
        handle.write('#!/bin/bash\nexec -a grok python3 -B "%s/fakegrok.py" "$@"\n' % HERE)
    os.chmod(os.path.join(bin_dir, "grok"), 0o755)
    config = os.path.join(work, "config.json")
    with open(config, "w") as handle:
        json.dump({"bind": "127.0.0.1", "port": int(port)}, handle)
    env = dict(os.environ, HOME=home, ERGOSPHERE_CONFIG=config, PATH=bin_dir + ":" + os.environ["PATH"])
    log = open(os.path.join(work, "server.log"), "w")
    server = subprocess.Popen(
        [sys.executable, "-B", os.path.abspath(__file__), "serve", code, fifo,
         "--bind", "127.0.0.1", "--port", str(port)],
        cwd=work, env=env, stdout=log, stderr=subprocess.STDOUT)
    base = "http://127.0.0.1:%s" % port
    checks = {}
    writer = None
    try:
        ready = lambda: "ERGOSPHERE_READY" in open(os.path.join(work, "server.log")).read()
        if not wait_for(ready):
            raise RuntimeError("server not ready")
        state = lambda sid: request(base, "/session-info?id=" + sid, timeout=60)[1].get("state", "")
        request(base, "/resume", {"id": other}, timeout=60)
        if not wait_for(lambda: state(other).startswith("running")):
            raise RuntimeError("the other session did not start")
        slow_reply = {}
        first = threading.Thread(target=lambda: slow_reply.update(
            zip(("code", "body"), request(base, "/resume", {"id": slow}, timeout=120))), daemon=True)
        first.start()
        if not wait_for(lambda: os.path.exists(fifo + ".entered")):
            raise RuntimeError("the slow start never reached its spawn")
        code_, body = request(base, "/session-info?id=" + other)
        checks["slow_state_read"] = check(code_ == 200 and str(body.get("state", "")).startswith("running"),
                                          "%s %s" % (code_, body.get("state") or body.get("error")))
        code_, body = request(base, "/resume", {"id": other})
        checks["slow_reattach"] = check(code_ == 200, "resume running: %s %s" % (code_, body.get("error", "")))
        code_, body = request(base, "/launch", {"cwd": os.path.join(work, "work"), "approval": "ask"})
        checks["slow_launch"] = check(code_ == 200 and body.get("ok") is True,
                                      "launch: %s %s" % (code_, body.get("error", "")))
        code_, body = request(base, "/resume", {"id": slow})
        checks["slow_second_start"] = check(code_ == 409 and body.get("state") == "starting",
                                            "second resume: %s %s" % (code_, body.get("state") or body.get("error")))
        writer = os.open(fifo, os.O_WRONLY)
        first.join(60)
        up = wait_for(lambda: state(slow).startswith("running"))
        checks["slow_one_grok"] = check(slow_reply.get("code") == 200 and up and groks_for(slow) == 1,
                                        "first %s, running %s, groks %d" % (slow_reply.get("code"), up, groks_for(slow)))
    except (RuntimeError, OSError) as exc:
        for name in ORDER:
            checks.setdefault(name, check(False, "not reached: %s" % exc))
    finally:
        if writer is None and os.path.exists(fifo + ".entered"):
            try:
                writer = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
            except OSError:
                pass
        server.terminate()
        try:
            server.wait(10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()
        if writer is not None:
            os.close(writer)
        log.close()
    return {"checks": checks}


def summary(apath, bpath):
    load = lambda p: (json.load(open(p)).get("checks") or {}) if os.path.exists(p) else {}
    a, b = load(apath), load(bpath)
    width = 36
    lines = ["", "Issue #45: a hung start of one session (folder on a stalled mount, stand-in fifo)",
             "%-18s %-*s %-*s %s" % ("check", width, "A", width, "B", "same")]
    regress = []
    for name in ORDER:
        ca = a.get(name) or {"ok": False, "value": "not run"}
        cb = b.get(name) or {"ok": False, "value": "not run"}
        cell = lambda c: ("%s %s" % ("PASS" if c["ok"] else "FAIL", c["value"]))[:width]
        if ca["ok"] and not cb["ok"]:
            regress.append(name)
        lines.append("%-18s %-*s %-*s %s" % (name, width, cell(ca), width, cell(cb), "yes" if ca == cb else "NO"))
    if regress:
        lines.append("REGRESSION: passes on A, fails on B: %s" % ", ".join(regress))
    print("\n".join(lines))
    return 1 if regress else 0


def main(argv):
    if len(argv) >= 4 and argv[1] == "serve":
        return serve(argv[2], argv[3], argv[4:])
    if len(argv) == 5 and argv[1] == "run":
        print(json.dumps(run(argv[2], argv[3], argv[4])))
        return 0
    if len(argv) == 4 and argv[1] == "summary":
        return summary(argv[2], argv[3])
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
