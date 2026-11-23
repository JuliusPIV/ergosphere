"""A session that ends while its page is open looks like one loaded ended (#55).

Loaded fresh, a stopped session's page shows a blank terminal.  A session that
ends while its page is open (Quit) must show the same blank terminal, not grok's
last frame.  Resume must not show that old frame again until the new grok draws
over it (the old frame, then black, then the new one was a flash).

Run: NODE_PATH=<node_modules with playwright> python3 -B tests/ended_view_test.py
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
const WAIT = 15000;
// Rows of the visible screen that hold any text.
const state = p => p.evaluate(() => {
  const buf = term.buffer.active;
  let text = 0;
  for (let y = 0; y < term.rows; y++) {
    const line = buf.getLine(buf.viewportY + y);
    if (line && line.translateToString(true).trim()) text += 1;
  }
  const last = buf.getLine(buf.viewportY + term.rows - 1);
  return {bar: document.getElementById('stop').textContent,
    status: document.getElementById('status').textContent,
    textRows: text, grok: !!last && / \$$/.test(last.translateToString(true).trim())};
});
async function until(p, pred) {
  const end = Date.now() + WAIT;
  let s = await state(p);
  while (!pred(s) && Date.now() < end) { await new Promise(r => setTimeout(r, 50)); s = await state(p); }
  return s;
}
const live = s => s.bar === 'Quit' && s.status === 'Running' && s.grok;
const ended = s => s.bar === 'Resume' && s.status === 'Ended';
(async () => {
  const b = await chromium.launch({executablePath: chrome});
  const res = {};
  try {
    const ctx = await b.newContext({viewport: {width: 1280, height: 800}});
    const p = await ctx.newPage();
    p.setDefaultTimeout(WAIT);
    await p.goto(url);
    res.fresh = await until(p, ended);
    await p.click('#resume-yes');
    res.live = await until(p, live);
    await p.click('#stop');
    res.ended = await until(p, ended);
    // Every frame from the Resume press until the new grok's first output
    // (status Running): how many rows of the old screen it still shows.
    await p.evaluate(() => {
      window.abFrames = [];
      const tick = () => {
        if (document.getElementById('status').textContent === 'Running') return;
        const buf = term.buffer.active;
        let text = 0;
        for (let y = 0; y < term.rows; y++) {
          const line = buf.getLine(buf.viewportY + y);
          if (line && line.translateToString(true).trim()) text += 1;
        }
        window.abFrames.push(text);
        requestAnimationFrame(tick);
      };
      document.getElementById('stop').addEventListener('click', () => requestAnimationFrame(tick), {once: true});
    });
    await p.click('#stop');
    res.resumed = await until(p, live);
    const frames = await p.evaluate(() => window.abFrames);
    res.frames = frames.length;
    res.oldFrames = frames.filter(n => n > 0).length;
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
    tmp = tempfile.mkdtemp(prefix="ergo-ended-view-")
    home = os.path.join(tmp, "home")
    work = os.path.join(tmp, "work")
    bindir = os.path.join(tmp, "bin")
    # A fresh id: the server looks for a session's holder machine-wide.
    sid = str(uuid.uuid4())
    sdir = os.path.join(home, ".grok", "sessions", "t", sid)
    for d in (sdir, work, bindir):
        os.makedirs(d)
    Path(sdir, "summary.json").write_text(json.dumps({"info": {"id": sid, "cwd": work},
        "generated_title": "ended view test", "updated_at": "2026-01-01T00:00:00Z"}))
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
                             capture_output=True, text=True, timeout=120, env=dict(os.environ, HOME=home))
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
    for step in ("live", "resumed"):
        s = res[step]
        if not (s["bar"] == "Quit" and s["status"] == "Running" and s["grok"]):
            return ["%s: grok is not live and drawn: %r" % (step, s)]
    for step in ("fresh", "ended"):
        if res[step]["bar"] != "Resume" or res[step]["status"] != "Ended":
            return ["%s: the page is not Ended: %r" % (step, res[step])]
    fails = []
    if res["ended"]["textRows"] != res["fresh"]["textRows"]:
        fails.append("ended while open shows %d rows of text, loaded ended shows %d" % (
            res["ended"]["textRows"], res["fresh"]["textRows"]))
    if res["oldFrames"]:
        fails.append("Resume showed the old screen in %d of %d frames before grok drew" % (
            res["oldFrames"], res["frames"]))
    return fails


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
