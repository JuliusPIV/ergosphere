"""Quit after the terminal socket failed ends the page (#56).

Resume starts grok, but the page's terminal socket closes before grok sent any
output: the page shows "terminal socket failed".  Pressing Quit then must not
sit on "quitting" forever: the page reconnects, sends the stop, grok quits and
the page shows Ended with Resume.

The failure is made with Playwright's WebSocket routing: the first /term socket
is closed at once, later ones go through to the server.

Run: NODE_PATH=<node_modules with playwright> python3 -B tests/quit_socket_failed_test.py
Stdlib only, no network, no real grok: "grok" is tests/ab/fakegrok.py.
Skipped without node, playwright or Chrome (/usr/bin/google-chrome or ERGO_CHROME).
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN_PORTS = {9241, 9242, 9119, 9120, 9121}

BROWSER_JS = r"""
const {chromium} = require('playwright');
const [url, chrome] = process.argv.slice(1);
const WAIT = 30000;
// After Quit the page should end within a second or two; "quitting" this long is the bug.
const QUIT_WAIT = 15000;
const state = p => p.evaluate(() => ({bar: document.getElementById('stop').textContent,
  status: document.getElementById('status').textContent}));
async function until(p, pred, wait) {
  const end = Date.now() + (wait || WAIT);
  let s = await state(p);
  while (!pred(s) && Date.now() < end) { await new Promise(r => setTimeout(r, 50)); s = await state(p); }
  return s;
}
(async () => {
  const b = await chromium.launch({executablePath: chrome});
  const res = {};
  try {
    const p = await (await b.newContext({viewport: {width: 1280, height: 800}})).newPage();
    p.setDefaultTimeout(WAIT);
    let sockets = 0;
    await p.routeWebSocket(u => u.pathname === '/term', ws => {
      sockets += 1;
      // The first socket fails before grok's first output; later ones reach the server.
      if (sockets === 1) ws.close();
      else ws.connectToServer();
    });
    await p.goto(url);
    res.ended = await until(p, s => s.bar === 'Resume' && s.status === 'Ended');
    await p.click('#stop');
    res.failed = await until(p, s => s.status === 'terminal socket failed');
    await p.click('#stop');
    res.quit = await until(p, s => s.status !== 'quitting', QUIT_WAIT);
    const sid = new URL(url).searchParams.get('resume');
    const info = new URL('/session-info?id=' + encodeURIComponent(sid), url).toString();
    // Stuck on "quitting" fails anyway: skip the wait for grok to stop.
    const end = res.quit.status === 'quitting' ? 0 : Date.now() + WAIT;
    let st = null;
    do {
      st = (await (await fetch(info)).json()).state;
      if (st === 'stopped') break;
      await new Promise(r => setTimeout(r, 50));
    } while (Date.now() < end);
    res.server = st;
    res.sockets = sockets;
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


def run_browser():
    node = shutil.which("node")
    chrome = os.environ.get("ERGO_CHROME", "/usr/bin/google-chrome")
    if not node or not os.path.exists(chrome):
        print("note: no node or Chrome, browser check skipped")
        return None
    if subprocess.run([node, "-e", "require('playwright')"], capture_output=True, timeout=20).returncode:
        print("note: no playwright module for node, browser check skipped")
        return None
    tmp = tempfile.mkdtemp(prefix="ergo-quit-failed-")
    home = os.path.join(tmp, "home")
    work = os.path.join(tmp, "work")
    bindir = os.path.join(tmp, "bin")
    # A fresh id: the server looks for a session's holder machine-wide.
    sid = str(uuid.uuid4())
    sdir = os.path.join(home, ".grok", "sessions", "t", sid)
    for d in (sdir, work, bindir):
        os.makedirs(d)
    # A stopped session: listed, but no grok runs it until Resume.
    Path(sdir, "summary.json").write_text(json.dumps({"info": {"id": sid, "cwd": work},
        "generated_title": "quit socket failed test", "updated_at": "2026-01-01T00:00:00Z"}))
    grok = os.path.join(bindir, "grok")
    # argv[0] must be "grok" for the server's /proc check.
    Path(grok).write_text('#!/bin/bash\nexec -a grok %s -B %s "$@"\n' % (
        sys.executable, ROOT / "tests" / "ab" / "fakegrok.py"))
    os.chmod(grok, 0o755)
    port = free_port()
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
            return {"error": "server did not start: %r" % line}
        url = "http://127.0.0.1:%d/chat?resume=%s" % (port, sid)
        out = subprocess.run([node, "-e", BROWSER_JS, url, chrome],
                             capture_output=True, text=True, timeout=180, env=dict(os.environ, HOME=home))
        try:
            return json.loads(out.stdout)
        except ValueError:
            return {"error": "node failed: %s" % out.stderr.strip()[-500:]}
    finally:
        srv.terminate()
        try:
            srv.wait(10)
        except subprocess.TimeoutExpired:
            srv.kill()
        shutil.rmtree(tmp, ignore_errors=True)


def check(res):
    if "error" in res:
        return ["browser: %s" % res["error"]]
    if res["ended"]["bar"] != "Resume" or res["ended"]["status"] != "Ended":
        return ["the stopped session's page is not Ended: %r" % res["ended"]]
    if res["failed"]["status"] != "terminal socket failed":
        return ["the closed socket did not show 'terminal socket failed': %r" % res["failed"]]
    if res["quit"]["status"] == "quitting":
        return ["Quit after the socket failed still shows 'quitting' after 15 s: %r" % res["quit"]]
    if res["quit"]["bar"] != "Resume" or res["quit"]["status"] != "Ended":
        return ["Quit after the socket failed did not end the page: %r" % res["quit"]]
    if res["server"] != "stopped":
        return ["grok still runs after Quit (server state %r)" % res["server"]]
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
