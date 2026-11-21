"""Resume in the bar closes the "Resume this session?" box (#57).

On an ended session a click on the terminal opens the box.  Pressing the bar's
Resume instead of the box's must close it: the session goes live, and a box
left over the live terminal would run Resume again and drop the new socket.

Run: NODE_PATH=<node_modules with playwright> python3 -B tests/resume_box_test.py
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
const state = p => p.evaluate(() => {
  const buf = typeof term !== 'undefined' && term.buffer ? term.buffer.active : null;
  const line = buf ? buf.getLine(buf.length - 1) : null;
  return {bar: document.getElementById('stop').textContent,
    status: document.getElementById('status').textContent,
    box: !document.getElementById('resume-ask').hidden,
    // The stand-in's status line, complete (it ends in " $"): grok is live.
    grok: !!line && /off=\d+\/\d+ size=\d+x\d+ n=\d+ last=\w+ \$/.test(line.translateToString(true))};
});
async function until(p, pred) {
  const end = Date.now() + WAIT;
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
    await p.goto(url);
    res.ended = await until(p, s => s.bar === 'Resume' && s.status === 'Ended');
    await p.click('#term', {position: {x: 20, y: 20}});
    res.asked = await until(p, s => s.box);
    await p.click('#stop');
    res.live = await until(p, s => s.grok);
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
    tmp = tempfile.mkdtemp(prefix="ergo-resume-box-")
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
        "generated_title": "resume box test", "updated_at": "2026-01-01T00:00:00Z"}))
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
    if res["ended"]["bar"] != "Resume" or res["ended"]["status"] != "Ended":
        return ["the stopped session's page is not Ended: %r" % res["ended"]]
    if not res["asked"]["box"]:
        return ["a click on the ended terminal did not open the resume box: %r" % res["asked"]]
    if not res["live"]["grok"]:
        return ["the bar's Resume did not bring the session live: %r" % res["live"]]
    if res["live"]["box"]:
        return ["the resume box is still open over the live terminal after the bar's Resume"]
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
