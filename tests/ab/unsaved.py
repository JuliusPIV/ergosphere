"""A/B check for issue #44: a new session whose grok exits before it saved.

  python3 -B unsaved.py run <code-dir> <port> <work-dir>  -> JSON checks on stdout
  python3 -B unsaved.py summary <A-json> <B-json>         -> table on stdout;
                                                             exit 1 when a check passes on A and fails on B,
                                                             or the browser checks did not run on B
  python3 -B unsaved.py grok <grok args...>               (the stand-in grok, used by run)

The stand-in grok prints one line.  A new session (--session-id) then waits
for <work>/ctl/<id> and exits without writing summary.json, as a grok killed
in its first 0.2 s does.  A resumed one (--resume) stays up until signalled.
The page is driven in headless Chromium (playwright), which creates the
control file once the page shows grok's output.  Stdlib plus node/playwright.
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
WAIT = 30
ORDER = ["unsaved_page_live", "unsaved_no_resume_ask", "unsaved_page_reload", "unsaved_quit",
         "unsaved_resume_reply", "saved_resume"]
BROWSER = ["unsaved_page_live", "unsaved_no_resume_ask", "unsaved_page_reload", "unsaved_quit"]

BROWSER_JS = r"""
const {chromium} = require('playwright');
const fs = require('fs');
const [base, sid, ctl, cwd, chrome] = process.argv.slice(1);
const WAIT = 30000;
const check = (ok, value) => ({ok: !!ok, value: String(value).slice(0, 60)});
const look = p => p.evaluate(() => ({
  status: document.getElementById('status').textContent,
  stop: document.getElementById('stop').hidden ? 'hidden' : document.getElementById('stop').textContent,
  fresh: !!document.getElementById('fresh') && !document.getElementById('fresh').hidden,
  ask: !document.getElementById('resume-ask').hidden,
  ended: typeof ended !== 'undefined' && ended,
  out: (() => { try { return term.buffer.active.getLine(0).translateToString(true); } catch (e) { return ''; } })()
}));
async function until(p, pred) {
  const end = Date.now() + WAIT;
  let s = await look(p);
  while (!pred(s) && Date.now() < end) { await new Promise(r => setTimeout(r, 50)); s = await look(p); }
  return s;
}
const unsaved = s => s.ended && /never saved/.test(s.status) && s.stop === 'hidden' && s.fresh;
const show = s => `${s.status.slice(0, 28)} | stop ${s.stop} | new ${s.fresh} | ask ${s.ask}`;
(async () => {
  const opts = chrome ? {executablePath: chrome} : {};
  const b = await chromium.launch(opts);
  const checks = {};
  try {
    const p = await b.newPage({viewport: {width: 1100, height: 700}});
    p.setDefaultTimeout(WAIT);
    await p.goto(`${base}/chat?resume=${sid}`);
    await until(p, s => /fake grok ready/.test(s.out));
    fs.writeFileSync(ctl, 'exit');
    // The page's terminal is live when grok exits: the ended frame decides.
    // The page shows unsaved in the same step that ends it: no second wait.
    let s = await until(p, s => s.ended);
    checks.unsaved_page_live = check(unsaved(s), show(s));
    // A click on an ended terminal opens "Resume this session?" (#52) on a
    // saved session.  Here it must not: its Resume can never work.
    await p.click('#term');
    await p.keyboard.press('a');
    s = await look(p);
    checks.unsaved_no_resume_ask = check(s.ended && !s.ask, show(s));
    await p.reload();
    s = await until(p, s => s.ended);
    checks.unsaved_page_reload = check(unsaved(s) && !s.ask, show(s));
    // Quit before grok saved: this tab's socket closes (Ended); its Resume
    // must then say unsaved, and the notice must survive the close.
    const id2 = await p.evaluate(async cwd => {
      const r = await fetch('/launch', {method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({cwd, approval: 'ask'})});
      return (await r.json()).id;
    }, cwd);
    await p.goto(`${base}/chat?resume=${id2}`);
    await until(p, s => /fake grok ready/.test(s.out));
    await p.click('#stop');
    s = await until(p, s => s.ended);
    if (!unsaved(s)) {
      await p.click('#stop');
      s = await until(p, unsaved);
    }
    checks.unsaved_quit = check(unsaved(s), show(s));
  } finally {
    await b.close();
  }
  console.log(JSON.stringify({browser: b.version(), checks}));
})().catch(e => { console.log(JSON.stringify({error: String(e && e.message || e).slice(0, 300)})); process.exit(0); });
"""


def grok(args):
    sys.stdout.write("fake grok ready\r\n")
    sys.stdout.flush()
    if "--resume" in args:
        while True:
            time.sleep(1)
    sid = args[args.index("--session-id") + 1] if "--session-id" in args else ""
    ctl = os.path.join(os.environ["UNSAVED_AB_CTL"], sid)
    end = time.monotonic() + 300
    while not os.path.exists(ctl) and time.monotonic() < end:
        time.sleep(0.02)
    return 0


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
        time.sleep(0.05)
    return False


def browser_off():
    if not shutil.which("node"):
        return "node not found"
    if subprocess.run(["node", "-e", "require.resolve('playwright')"], capture_output=True).returncode:
        return "playwright module not found (set NODE_PATH)"
    return ""


def run(code, port, work):
    home, bin_dir, ctl = (os.path.join(work, d) for d in ("home", "bin", "ctl"))
    for d in (bin_dir, ctl, os.path.join(work, "work")):
        os.makedirs(d)
    saved = str(uuid.uuid4())
    folder = os.path.join(home, ".grok", "sessions", "ab", saved)
    os.makedirs(folder)
    with open(os.path.join(folder, "summary.json"), "w") as handle:
        json.dump({"info": {"id": saved, "cwd": os.path.join(work, "work")}, "generated_title": "AB saved",
                   "updated_at": "2026-01-01T00:00:00Z"}, handle)
    with open(os.path.join(bin_dir, "grok"), "w") as handle:
        handle.write('#!/bin/bash\nexec -a grok python3 -B "%s" grok "$@"\n' % os.path.abspath(__file__))
    os.chmod(os.path.join(bin_dir, "grok"), 0o755)
    config = os.path.join(work, "config.json")
    with open(config, "w") as handle:
        json.dump({"bind": "127.0.0.1", "port": int(port)}, handle)
    env = dict(os.environ, HOME=home, ERGOSPHERE_CONFIG=config, UNSAVED_AB_CTL=ctl,
               PATH=bin_dir + ":" + os.environ["PATH"])
    log = open(os.path.join(work, "server.log"), "w")
    server = subprocess.Popen([sys.executable, "-B", os.path.join(code, "ergosphere.py"),
                               "--bind", "127.0.0.1", "--port", str(port)],
                              cwd=work, env=env, stdout=log, stderr=subprocess.STDOUT)
    base = "http://127.0.0.1:%s" % port
    checks, out = {}, {}
    try:
        if not wait_for(lambda: "ERGOSPHERE_READY" in open(os.path.join(work, "server.log")).read()):
            raise RuntimeError("server not ready")
        code_, body = request(base, "/launch", {"cwd": os.path.join(work, "work"), "approval": "ask"})
        sid = body.get("id")
        if code_ != 200 or not sid:
            raise RuntimeError("launch: %s %s" % (code_, body.get("error")))
        why = browser_off()
        if why:
            out["unavailable"] = why
            open(os.path.join(ctl, sid), "w").close()
        else:
            chrome = os.environ.get("AB_CHROME", "")
            res = subprocess.run(["node", "-e", BROWSER_JS, base, sid, os.path.join(ctl, sid),
                                  os.path.join(work, "work"), chrome],
                                 capture_output=True, text=True, timeout=240)
            try:
                got = json.loads(res.stdout.strip().splitlines()[-1])
            except (ValueError, IndexError):
                got = {"error": "node: %s" % res.stderr.strip()[-200:]}
            out.update({k: v for k, v in got.items() if k != "checks"})
            checks.update(got.get("checks") or {})
            open(os.path.join(ctl, sid), "a").close()
        gone = lambda: request(base, "/session-info")[1].get("running") == 0
        wait_for(gone)
        code_, body = request(base, "/resume", {"id": sid})
        checks["unsaved_resume_reply"] = check(code_ == 404 and body.get("unsaved") is True,
                                               "%s %s" % (code_, body.get("error", "")))
        code_, body = request(base, "/resume", {"id": saved})
        checks["saved_resume"] = check(code_ == 200, "%s %s" % (code_, body.get("error", "")))
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        out.setdefault("error", str(exc)[:200])
    finally:
        server.terminate()
        try:
            server.wait(10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()
        log.close()
    for name in ORDER:
        if name not in BROWSER or out.get("browser"):
            checks.setdefault(name, check(False, "not reached: %s" % out.get("error", "")))
    out["checks"] = checks
    return out


def summary(apath, bpath):
    load = lambda p: json.load(open(p)) if os.path.exists(p) else {}
    a, b = load(apath), load(bpath)
    width = 36
    lines = ["", "Issue #44: a new session whose grok exits before it saved (stand-in grok)"]
    if a.get("browser") and b.get("browser"):
        lines.append("browser: headless Chromium %s ran the page checks" % b["browser"])
    else:
        why = lambda r: str(r.get("browser") and "ran" or r.get("unavailable") or r.get("error") or "no result")
        lines.append("BROWSER CHECKS DID NOT RUN (A: %s; B: %s)" % (why(a)[:120], why(b)[:120]))
    lines.append("%-22s %-*s %-*s %s" % ("check", width, "A", width, "B", "same"))
    ca_all, cb_all = a.get("checks") or {}, b.get("checks") or {}
    regress = []
    for name in ORDER:
        ca = ca_all.get(name) or {"ok": False, "value": "not run"}
        cb = cb_all.get(name) or {"ok": False, "value": "not run"}
        cell = lambda c: ("%s %s" % ("PASS" if c["ok"] else "FAIL", c["value"]))[:width]
        if ca["ok"] and not cb["ok"]:
            regress.append(name)
        lines.append("%-22s %-*s %-*s %s" % (name, width, cell(ca), width, cell(cb), "yes" if ca == cb else "NO"))
    broken = not b.get("browser") and not b.get("unavailable")
    if regress:
        lines.append("REGRESSION: passes on A, fails on B: %s" % ", ".join(regress))
    if broken:
        lines.append("BROWSER FAILURE: %s" % str(b.get("error") or "no result")[:200])
    print("\n".join(lines))
    return 1 if regress or broken else 0


def main(argv):
    if len(argv) >= 2 and argv[1] == "grok":
        return grok(argv[2:])
    if len(argv) == 5 and argv[1] == "run":
        print(json.dumps(run(argv[2], argv[3], argv[4])))
        return 0
    if len(argv) == 4 and argv[1] == "summary":
        return summary(argv[2], argv[3])
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
