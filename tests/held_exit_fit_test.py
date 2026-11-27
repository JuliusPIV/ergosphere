"""A held page fits its terminal when the other grok exits (#186).

A session page opened while a grok outside this server holds the session shows
the "already running" notice.  When that grok exits and this server does not
run the session, the page shows Ended: its terminal must fit the page then, not
keep xterm's default 80x24 (the load path got that fit in #52).

The holder is a stand-in process whose argv[0] is "grok" and whose command line
names the session id (`grok -r <id>`), as the server's /proc scan looks for.

Run: NODE_PATH=<node_modules with playwright> python3 -B tests/held_exit_fit_test.py
Stdlib only, no network, no real grok.  Uses ERGOSPHERE_TEST_PORT when set.
Skipped without node, playwright or Chrome (/usr/bin/google-chrome or ERGO_CHROME).
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN_PORTS = {9241, 9242, 9119, 9120, 9121}

# Prints "held" once the page shows the holder's notice, then reads one line
# on stdin (the test has stopped the holder) and prints the page's state at the
# moment it shows Ended.  The fit, when the page makes one, runs in the same
# step that shows Ended, so that reading is the one to judge.
BROWSER_JS = r"""
const {chromium} = require('playwright');
const [url, chrome] = process.argv.slice(1);
const WAIT = 20000;
const state = p => p.evaluate(() => {
  let want = null;
  try { want = fit.proposeDimensions() || null; } catch (err) {}
  return {hold: hold, ended: ended,
    status: document.getElementById('status').textContent,
    busy: document.getElementById('busy').style.display !== 'none',
    cols: term.cols, rows: term.rows,
    want: want ? {cols: want.cols, rows: want.rows} : null};
});
async function until(p, pred) {
  const end = Date.now() + WAIT;
  let s = await state(p);
  while (!pred(s) && Date.now() < end) { await new Promise(r => setTimeout(r, 25)); s = await state(p); }
  return s;
}
const line = () => new Promise(r => process.stdin.once('data', r));
(async () => {
  const b = await chromium.launch({executablePath: chrome});
  const res = {};
  try {
    const ctx = await b.newContext({viewport: {width: 1280, height: 800}});
    const p = await ctx.newPage();
    p.setDefaultTimeout(WAIT);
    await p.goto(url);
    res.held = await until(p, s => s.hold && s.busy);
    console.log('held');
    await line();
    res.ended = await until(p, s => s.ended);
  } finally {
    await b.close();
  }
  console.log(JSON.stringify(res));
  process.exit(0);
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


def held_by_server(port, sid):
    url = "http://127.0.0.1:%d/holder?id=%s" % (port, sid)
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.loads(resp.read().decode()).get("held")


def run_browser():
    node = shutil.which("node")
    chrome = os.environ.get("ERGO_CHROME", "/usr/bin/google-chrome")
    if not node or not os.path.exists(chrome):
        print("note: no node or Chrome, browser check skipped")
        return None
    if subprocess.run([node, "-e", "require('playwright')"], capture_output=True, timeout=20).returncode:
        print("note: no playwright module for node, browser check skipped")
        return None
    tmp = tempfile.mkdtemp(prefix="ergo-held-exit-fit-")
    home = os.path.join(tmp, "home")
    work = os.path.join(tmp, "work")
    # A fresh id: the server looks for a session's holder machine-wide.
    sid = str(uuid.uuid4())
    sdir = os.path.join(home, ".grok", "sessions", "t", sid)
    bindir = os.path.join(tmp, "bin")
    for d in (sdir, work, bindir):
        os.makedirs(d)
    # The server will not start without a grok on PATH; this one never runs here.
    grok = os.path.join(bindir, "grok")
    Path(grok).write_text('#!/bin/bash\nexec -a grok %s -B %s "$@"\n' % (
        sys.executable, ROOT / "tests" / "ab" / "fakegrok.py"))
    os.chmod(grok, 0o755)
    Path(sdir, "summary.json").write_text(json.dumps({"info": {"id": sid, "cwd": work},
        "generated_title": "held exit fit test", "updated_at": "2026-01-01T00:00:00Z"}))
    port = free_port()
    cfg = os.path.join(tmp, "ergosphere.json")
    Path(cfg).write_text(json.dumps({"bind": "127.0.0.1", "port": port}))
    env = dict(os.environ, HOME=home, ERGOSPHERE_CONFIG=cfg,
               PATH=bindir + os.pathsep + os.environ.get("PATH", ""))
    env.pop("ERGOSPHERE_TOKEN", None)
    env.pop("ERGOSPHERE_PORT", None)
    # The other grok: not started by this server, the id on its command line.
    holder = subprocess.Popen(["grok", "-c", "import time; time.sleep(600)", "-r", sid],
                              executable=sys.executable, cwd=tmp)
    srv = subprocess.Popen([sys.executable, "-B", str(ROOT / "ergosphere.py")], env=env, cwd=tmp,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    page = None
    try:
        line = srv.stdout.readline()
        if "ERGOSPHERE_READY" not in line:
            return {"error": "server did not start: %r" % line}
        if held_by_server(port, sid) is not True:
            return {"error": "the stand-in holder is not seen as holding the session"}
        url = "http://127.0.0.1:%d/chat?resume=%s" % (port, sid)
        page = subprocess.Popen([node, "-e", BROWSER_JS, url, chrome], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                env=dict(os.environ, HOME=home))
        first = page.stdout.readline()
        if first.strip() != "held":
            page.kill()
            return {"error": "page never showed the held notice: %r %s" % (
                first, page.stderr.read().strip()[-500:])}
        holder.kill()
        holder.wait(10)
        # The server sees the holder gone before the page is told to watch.
        end = time.monotonic() + 30
        while held_by_server(port, sid) is not False and time.monotonic() < end:
            time.sleep(0.05)
        out, err = page.communicate("go\n", timeout=120)
        try:
            return json.loads(out.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return {"error": "node failed: %s" % err.strip()[-500:]}
    finally:
        if page is not None and page.poll() is None:
            page.kill()
        if holder.poll() is None:
            holder.kill()
            holder.wait(10)
        srv.terminate()
        try:
            srv.wait(10)
        except subprocess.TimeoutExpired:
            srv.kill()
        shutil.rmtree(tmp, ignore_errors=True)


def check(res):
    if "error" in res:
        return ["browser: %s" % res["error"]]
    e = res["ended"]
    if not e["ended"] or e["status"] != "Ended":
        return ["the page did not show Ended after the holder exited: %r" % e]
    if not e["want"] or e["cols"] != e["want"]["cols"] or e["rows"] != e["want"]["rows"]:
        return ["terminal not fitted when Ended: %sx%s, page fits %r" % (e["cols"], e["rows"], e["want"])]
    return []


def main():
    res = run_browser()
    if res is None:
        print("SKIP browser")
        return 0
    print("browser: %s" % json.dumps(res))
    fails = check(res)
    for f in fails:
        print("FAIL %s" % f)
    print("PASS" if not fails else "FAIL")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
