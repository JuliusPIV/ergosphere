"""A/B check for issue #42: unchanged polls must not scan all of /proc each time.

  python3 -B poll_cost.py run <code-dir> <port> <work-dir>  -> JSON checks on stdout
  python3 -B poll_cost.py summary <A-json> <B-json>          -> table on stdout;
                                                              exit 1 when a check passes on A and fails on B
  python3 -B poll_cost.py serve <code-dir> <count-file> <server args...>  (used by run)

serve runs that side's ergosphere.py with one change: each full /proc pass
(ProcScan) appends a line to the count file.  Ten stand-in CLI groks (argv[0]
"grok", a session id on the command line) hold ten seeded sessions, as in the
issue's measurement.  Stdlib only.
"""
import importlib.util
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

POLLS = 10
ORDER = ["poll_reuse", "poll_sees_start", "poll_list_sees_start", "poll_sees_quit"]


def serve(code, count_file, args):
    spec = importlib.util.spec_from_file_location("ergosphere", os.path.join(code, "ergosphere.py"))
    erg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(erg)
    real = erg.ProcScan

    class Counting(real):
        def __init__(self):
            with open(count_file, "a") as handle:
                handle.write("1\n")
            super().__init__()

    erg.ProcScan = Counting
    return erg.main(args)


def request(base, path, headers=None):
    req = urllib.request.Request(base + path, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def check(ok, value):
    return {"ok": bool(ok), "value": str(value)[:60]}


def wait_for(fn, deadline=60):
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        if fn():
            return True
        time.sleep(0.05)
    return False


def seed(home, sid, cwd):
    folder = os.path.join(home, ".grok", "sessions", "ab", sid)
    os.makedirs(folder)
    os.makedirs(cwd, exist_ok=True)
    with open(os.path.join(folder, "summary.json"), "w") as handle:
        json.dump({"info": {"id": sid, "cwd": cwd}, "generated_title": "AB poll " + sid[:8],
                   "updated_at": "2026-01-01T00:00:00Z"}, handle)


def cli_grok(sid):
    # A grok typed in a shell, as /proc shows it: argv[0] grok, the id after -r.
    return subprocess.Popen(["bash", "-c", 'exec -a grok python3 -B -c "import time; time.sleep(600)" -r "$0"', sid],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def is_grok_for(pid, sid):
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as handle:
            parts = handle.read().split(b"\0")
    except OSError:
        return False
    return parts[0] == b"grok" and sid.encode() in parts


def median_ms(samples):
    samples = sorted(samples)
    return samples[len(samples) // 2] * 1000.0


def run(code, port, work):
    home = os.path.join(work, "home")
    count_file = os.path.join(work, "scans")
    sids = [str(uuid.uuid4()) for _ in range(11)]
    for sid in sids:
        seed(home, sid, os.path.join(work, "work"))
    config = os.path.join(work, "config.json")
    with open(config, "w") as handle:
        json.dump({"bind": "127.0.0.1", "port": int(port)}, handle)
    env = dict(os.environ, HOME=home, ERGOSPHERE_CONFIG=config)
    log = open(os.path.join(work, "server.log"), "w")
    server = subprocess.Popen(
        [sys.executable, "-B", os.path.abspath(__file__), "serve", code, count_file,
         "--bind", "127.0.0.1", "--port", str(port)],
        cwd=work, env=env, stdout=log, stderr=subprocess.STDOUT)
    base = "http://127.0.0.1:%s" % port
    scans = lambda: len(open(count_file).read().split()) if os.path.exists(count_file) else 0
    state = lambda sid: json.loads(request(base, "/session-info?id=" + sid)[2] or b"{}").get("state", "")
    checks, info, groks = {}, {}, []
    try:
        ready = lambda: "ERGOSPHERE_READY" in open(os.path.join(work, "server.log")).read()
        if not wait_for(ready):
            raise RuntimeError("server not ready")
        groks = [cli_grok(sid) for sid in sids[:10]]
        if not wait_for(lambda: all(is_grok_for(p.pid, s) for p, s in zip(groks, sids)) and
                        state(sids[0]) == "held-by-cli"):
            raise RuntimeError("stand-in groks not seen")
        _code, head, _body = request(base, "/list-rows")
        tag = head.get("X-Rows-Tag", "")
        request(base, "/session-info?id=" + sids[0])
        before = scans()
        info_t, rows_t, codes = [], [], set()
        for _ in range(POLLS):
            start = time.perf_counter()
            request(base, "/session-info?id=" + sids[0])
            info_t.append(time.perf_counter() - start)
            start = time.perf_counter()
            code_, _h, _b = request(base, "/list-rows", {"X-Rows-Tag": tag})
            rows_t.append(time.perf_counter() - start)
            codes.add(code_)
        passes = scans() - before
        info.update(info_ms=round(median_ms(info_t), 2), rows_ms=round(median_ms(rows_t), 2),
                    passes=passes, polls=2 * POLLS)
        # A scans on every poll (exactly 2 * POLLS). B scans again only when a
        # grok or turn on this machine changed, so fewer than every poll.
        checks["poll_reuse"] = check(passes < 2 * POLLS and codes == {204},
                                     "%d passes/%d polls, info %.1fms rows %.1fms"
                                     % (passes, 2 * POLLS, info["info_ms"], info["rows_ms"]))
        new = sids[10]
        late = cli_grok(new)
        groks.append(late)
        wait_for(lambda: is_grok_for(late.pid, new))
        got = state(new)
        checks["poll_sees_start"] = check(got == "held-by-cli", "first poll after start: %s" % got)
        code_, _h, _b = request(base, "/list-rows", {"X-Rows-Tag": tag})
        checks["poll_list_sees_start"] = check(code_ == 200, "/list-rows with old tag: %s" % code_)
        late.kill()
        late.wait()
        got = state(new)
        checks["poll_sees_quit"] = check(got == "stopped", "first poll after quit: %s" % got)
    except (RuntimeError, OSError, ValueError) as exc:
        for name in ORDER:
            checks.setdefault(name, check(False, "not reached: %s" % exc))
    finally:
        for proc in groks:
            proc.kill()
            proc.wait()
        server.terminate()
        try:
            server.wait(10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()
        log.close()
    return {"checks": checks, "info": info}


def summary(apath, bpath):
    load = lambda p: json.load(open(p)) if os.path.exists(p) and os.path.getsize(p) else {}
    a, b = load(apath), load(bpath)
    width = 44
    lines = ["", "Issue #42: polls with ten stand-in CLI groks; median of %d polls each" % POLLS,
             "%-20s %-*s %-*s %s" % ("check", width, "A", width, "B", "same")]
    regress = []
    for name in ORDER:
        ca = (a.get("checks") or {}).get(name) or {"ok": False, "value": "not run"}
        cb = (b.get("checks") or {}).get(name) or {"ok": False, "value": "not run"}
        cell = lambda c: ("%s %s" % ("PASS" if c["ok"] else "FAIL", c["value"]))[:width]
        if ca["ok"] and not cb["ok"]:
            regress.append(name)
        lines.append("%-20s %-*s %-*s %s" % (name, width, cell(ca), width, cell(cb), "yes" if ca == cb else "NO"))
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
