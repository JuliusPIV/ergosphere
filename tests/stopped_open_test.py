"""Opening a stopped session shows a fitted terminal and the resume prompt (#52).

A stopped session's page, loaded fresh, must fit the terminal to the page (not
xterm's default 80x24) and open the "Resume this session?" box at once, with
its Resume button focused, before any click.  A stopped session whose folder
is gone (#43) gets the fitted terminal but no automatic box: its Resume always
fails.

Run: NODE_PATH=<node_modules with playwright> python3 -B tests/stopped_open_test.py
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
const [url, goneUrl, chrome] = process.argv.slice(1);
const WAIT = 15000;
const state = p => p.evaluate(() => {
  let want = null;
  try { want = fit.proposeDimensions() || null; } catch (err) {}
  return {bar: document.getElementById('stop').textContent,
    status: document.getElementById('status').textContent,
    box: !document.getElementById('resume-ask').hidden,
    focus: document.activeElement ? document.activeElement.id : '',
    cols: term.cols, rows: term.rows,
    want: want ? {cols: want.cols, rows: want.rows} : null};
});
const fitted = s => !!s.want && s.cols === s.want.cols && s.rows === s.want.rows;
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
    const ctx = await b.newContext({viewport: {width: 1280, height: 800}});
    const p = await ctx.newPage();
    p.setDefaultTimeout(WAIT);
    await p.goto(url);
    res.ended = await until(p, s => s.bar === 'Resume' && s.status === 'Ended');
    // No click: the page alone must fit the terminal and ask.
    res.open = await until(p, s => fitted(s) && s.box && s.focus === 'resume-yes');
    res.open.fitted = fitted(res.open);
    // Folder gone: the box would open in the same synchronous load step that
    // fits the terminal, so none by the time it is Ended and fitted means none.
    const g = await ctx.newPage();
    g.setDefaultTimeout(WAIT);
    await g.goto(goneUrl);
    res.gone = await until(g, s => s.status === 'Ended' && fitted(s));
    res.gone.fitted = fitted(res.gone);
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
    tmp = tempfile.mkdtemp(prefix="ergo-stopped-open-")
    home = os.path.join(tmp, "home")
    work = os.path.join(tmp, "work")
    bindir = os.path.join(tmp, "bin")
    # A fresh id: the server looks for a session's holder machine-wide.
    sid = str(uuid.uuid4())
    gone_sid = str(uuid.uuid4())
    sdir = os.path.join(home, ".grok", "sessions", "t", sid)
    gone_dir = os.path.join(home, ".grok", "sessions", "t", gone_sid)
    for d in (sdir, gone_dir, work, bindir):
        os.makedirs(d)
    # Stopped sessions: listed, but no grok runs them.  The second one's folder
    # does not exist.
    Path(sdir, "summary.json").write_text(json.dumps({"info": {"id": sid, "cwd": work},
        "generated_title": "stopped open test", "updated_at": "2026-01-01T00:00:00Z"}))
    Path(gone_dir, "summary.json").write_text(json.dumps({"info": {"id": gone_sid,
        "cwd": os.path.join(tmp, "gone")}, "generated_title": "stopped open test, folder gone",
        "updated_at": "2026-01-01T00:00:00Z"}))
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
        gone_url = "http://127.0.0.1:%d/chat?resume=%s" % (port, gone_sid)
        out = subprocess.run([node, "-e", BROWSER_JS, url, gone_url, chrome],
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
    s = res["open"]
    fails = []
    if not s["fitted"]:
        fails.append("terminal not fitted: %sx%s, page fits %r" % (s["cols"], s["rows"], s["want"]))
    if not s["box"]:
        fails.append("the resume box did not open on load")
    elif s["focus"] != "resume-yes":
        fails.append("Resume is not focused: %r" % s["focus"])
    g = res["gone"]
    if not g["fitted"]:
        fails.append("folder gone: terminal not fitted: %sx%s, page fits %r" % (g["cols"], g["rows"], g["want"]))
    if g["box"]:
        fails.append("folder gone: the resume box opened on load, but its Resume always fails")
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
