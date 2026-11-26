"""grok starts at the page's terminal size, not 80x24 (#58).

A new session (Create on the list page) and a resumed one (Quit, then Resume
on the chat page) must start grok on a PTY of the size the chat page's
terminal fits to, so grok's first frame fills the page.  A /launch or /resume
that sends no size still starts at 80x24, and a size out of bounds is clamped
as /term's is.

The stand-in grok logs the PTY size it was started with (`stty size`, before
it draws anything), with its arguments, so each start can be matched to its
session id.

Run: NODE_PATH=<node_modules with playwright> python3 -B tests/start_size_test.py
Stdlib only, no network, no real grok: "grok" is tests/ab/fakegrok.py.
The browser part is skipped without node, playwright or Chrome
(/usr/bin/google-chrome or ERGO_CHROME); the HTTP part always runs.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN_PORTS = {9241, 9242, 9119, 9120, 9121}
WAIT = 30

BROWSER_JS = r"""
const {chromium} = require('playwright');
const [base, sid, cwd, chrome] = process.argv.slice(1);
const WAIT = 30000;
const state = p => p.evaluate(() => {
  const buf = typeof term !== 'undefined' && term.buffer ? term.buffer.active : null;
  const line = buf ? buf.getLine(buf.length - 1) : null;
  return {bar: document.getElementById('stop').textContent,
    status: document.getElementById('status').textContent,
    cols: term.cols, rows: term.rows,
    // The stand-in's status line, complete (it ends in " $"): grok is live.
    grok: !!line && /off=\d+\/\d+ size=\d+x\d+ n=\d+ last=\w+ \$/.test(line.translateToString(true))};
});
async function until(p, pred) {
  const end = Date.now() + WAIT;
  let s = await state(p);
  while (!pred(s) && Date.now() < end) { await new Promise(r => setTimeout(r, 50)); s = await state(p); }
  return s;
}
async function launch(b, width, height) {
  const ctx = await b.newContext({viewport: {width, height}});
  try {
    const p = await ctx.newPage();
    p.setDefaultTimeout(WAIT);
    await p.goto(base + '/');
    await p.click('a[data-kind="new"]');
    await p.fill('#na-cwd', cwd);
    await p.click('#na-create');
    await p.waitForURL(/\/chat\?resume=/);
    const id = new URL(p.url()).searchParams.get('resume');
    const s = await until(p, s => s.grok);
    return {id, cols: s.cols, rows: s.rows, live: s.grok};
  } finally {
    await ctx.close();
  }
}
(async () => {
  const b = await chromium.launch({executablePath: chrome});
  const res = {};
  try {
    res.desktop = await launch(b, 1280, 800);
    res.phone = await launch(b, 390, 700);
    const ctx = await b.newContext({viewport: {width: 1100, height: 700}});
    const p = await ctx.newPage();
    p.setDefaultTimeout(WAIT);
    await p.goto(base + '/chat?resume=' + sid);
    let s = await until(p, s => s.bar === 'Resume' && s.status === 'Ended');
    await p.click('#stop');
    s = await until(p, s => s.grok);
    res.resume = {id: sid, cols: s.cols, rows: s.rows, live: s.grok};
    await ctx.close();
  } finally {
    await b.close();
  }
  console.log(JSON.stringify(res));
})().catch(e => { console.error(String(e && e.stack || e)); process.exit(1); });
"""


def free_port():
    if os.environ.get("ERGOSPHERE_TEST_PORT"):
        return int(os.environ["ERGOSPHERE_TEST_PORT"])
    while True:
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        if port not in FORBIDDEN_PORTS:
            return port


def post(base, path, body):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(), headers={
        "Origin": base, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, {}


def starts(log, sid):
    # Every logged start of sid: [(rows, cols), ...] in order.
    out = []
    try:
        text = Path(log).read_text()
    except OSError:
        return out
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and sid in parts[2:]:
            out.append((int(parts[0]), int(parts[1])))
    return out


def wait_start(log, sid, count):
    end = time.monotonic() + WAIT
    while time.monotonic() < end:
        got = starts(log, sid)
        if len(got) >= count:
            return got[count - 1]
        time.sleep(0.05)
    return None


def stop(base, sid):
    # The chat page's Quit, over the socket, is not needed here: /resume
    # sizes are read at spawn.  Kill the stand-in by its session id.
    subprocess.run(["pkill", "-f", "--", sid], capture_output=True)
    end = time.monotonic() + WAIT
    while time.monotonic() < end:
        with urllib.request.urlopen(base + "/session-info?id=" + sid, timeout=60) as resp:
            if json.loads(resp.read().decode("utf-8")).get("state") == "stopped":
                return True
        time.sleep(0.05)
    return False


def run_browser(base, sid, cwd, home):
    node = shutil.which("node")
    chrome = os.environ.get("ERGO_CHROME", "/usr/bin/google-chrome")
    if not node or not os.path.exists(chrome):
        print("note: no node or Chrome, browser check skipped")
        return None
    if subprocess.run([node, "-e", "require('playwright')"], capture_output=True, timeout=20).returncode:
        print("note: no playwright module for node, browser check skipped")
        return None
    out = subprocess.run([node, "-e", BROWSER_JS, base, sid, cwd, chrome],
                         capture_output=True, text=True, timeout=180, env=dict(os.environ, HOME=home))
    try:
        return json.loads(out.stdout)
    except ValueError:
        return {"error": "node failed: %s" % out.stderr.strip()[-500:]}


def main():
    fails = []

    def check(cond, msg):
        print(("ok:   " if cond else "FAIL: ") + msg)
        if not cond:
            fails.append(msg)

    tmp = tempfile.mkdtemp(prefix="ergo-start-size-")
    home = os.path.join(tmp, "home")
    work = os.path.join(tmp, "work")
    bindir = os.path.join(tmp, "bin")
    log = os.path.join(tmp, "starts.log")
    # Fresh ids: the server looks for a session's holder machine-wide.
    sid = str(uuid.uuid4())
    plain = str(uuid.uuid4())
    for one in (sid, plain):
        sdir = os.path.join(home, ".grok", "sessions", "t", one)
        os.makedirs(sdir)
        Path(sdir, "summary.json").write_text(json.dumps({"info": {"id": one, "cwd": work},
            "generated_title": "start size test", "updated_at": "2026-01-01T00:00:00Z"}))
    os.makedirs(work)
    os.makedirs(bindir)
    grok = os.path.join(bindir, "grok")
    # argv[0] must be "grok" for the server's /proc check.  stdin is the PTY.
    Path(grok).write_text('#!/bin/bash\necho "$(stty size) $*" >> %s\nexec -a grok %s -B %s "$@"\n' % (
        log, sys.executable, ROOT / "tests" / "ab" / "fakegrok.py"))
    os.chmod(grok, 0o755)
    port = free_port()
    base = "http://127.0.0.1:%d" % port
    cfg = os.path.join(tmp, "ergosphere.json")
    Path(cfg).write_text(json.dumps({"bind": "127.0.0.1", "port": port}))
    env = dict(os.environ, HOME=home, ERGOSPHERE_CONFIG=cfg,
               PATH=bindir + os.pathsep + os.environ.get("PATH", ""))
    env.pop("ERGOSPHERE_TOKEN", None)
    env.pop("ERGOSPHERE_PORT", None)
    srv = subprocess.Popen([sys.executable, "-B", str(ROOT / "ergosphere.py")], env=env, cwd=tmp,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        line = srv.stdout.readline()
        if "ERGOSPHERE_READY" not in line:
            print("FAIL server did not start: %r" % line)
            return 1

        # No size sent: 80x24, as before.  Out of bounds: clamped like /term.
        code, _ = post(base, "/resume", {"id": plain})
        check(code == 200 and wait_start(log, plain, 1) == (24, 80),
              "/resume with no size starts 80x24: %s %s" % (code, starts(log, plain)))
        check(stop(base, plain), "stand-in stopped")
        code, _ = post(base, "/resume", {"id": plain, "cols": 9999, "rows": "40"})
        check(code == 200 and wait_start(log, plain, 2) == (24, 500),
              "/resume clamps cols to 500 and ignores a non-number rows: %s %s" % (code, starts(log, plain)))
        check(stop(base, plain), "stand-in stopped")
        code, body = post(base, "/launch", {"cwd": work, "approval": "ask", "cols": 120, "rows": 30})
        new = body.get("id", "")
        check(code == 200 and wait_start(log, new, 1) == (30, 120),
              "/launch with 120x30 starts 120x30: %s %s" % (code, starts(log, new)))

        res = run_browser(base, sid, work, home)
        if res is None:
            print("SKIP browser")
        elif "error" in res:
            check(False, "browser: %s" % res["error"])
        else:
            print("browser: %s" % json.dumps(res))
            for name in ("desktop", "phone", "resume"):
                r = res[name]
                first = starts(log, r["id"])
                first = first[-1] if first else None
                check(r["live"] and first == (r["rows"], r["cols"]),
                      "%s: grok started at %s, the page's terminal is %sx%s" % (
                          name, "x".join(map(str, first)) if first else "-", r["rows"], r["cols"]))
    finally:
        srv.terminate()
        try:
            srv.wait(10)
        except subprocess.TimeoutExpired:
            srv.kill()
        shutil.rmtree(tmp, ignore_errors=True)
    print("PASS" if not fails else "FAIL")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
