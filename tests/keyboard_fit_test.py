"""With a phone's on-screen keyboard open, grok's last line stays above it (#119).

A phone keyboard shrinks only the visual viewport (Chrome on Android since
108, Safari on iOS always), so the page's layout keeps its full height and
the bottom rows of the terminal sit under the keyboard.  Headless Chrome has
no on-screen keyboard: this test stands in for one by overriding
visualViewport.height and offsetTop and firing visualViewport's resize and
scroll events, which is all the page can observe of a real keyboard.

Per device profile (Pixel 7: keyboard 320px, no pan; iPhone 14 viewport in
Chrome: keyboard 300px, visual viewport panned to the bottom as iOS does to
show the focused field): tap the terminal, open the keyboard, then close it.
Open: the terminal has fewer rows, grok redrew at that size, and the
terminal's first and last rows lie inside the visible area.  Closed: the rows are
back to the full-height count.

Run: python3 -B tests/keyboard_fit_test.py
Needs node's playwright module (NODE_PATH) and Chrome at /usr/bin/google-chrome
(or ERGO_CHROME); skipped without them.  No network, no real grok: the
stand-in TUI is tests/ab/fakegrok.py.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN_PORTS = {9241, 9242, 9119, 9120, 9121}
SID = "11911911-0b0a-4908-8706-050403020119"

BROWSER_JS = r"""
const {chromium, devices} = require('playwright');
const [url, chrome] = process.argv.slice(1);
const wait = ms => new Promise(r => setTimeout(r, ms));
// Real height and offsetTop unless a stand-in keyboard is set.
function fakeKeyboard() {
  window.KB = null;
  const P = VisualViewport.prototype;
  for (const k of ['height', 'offsetTop']) {
    const real = Object.getOwnPropertyDescriptor(P, k).get;
    Object.defineProperty(P, k, {configurable: true,
      get() { return window.KB ? window.KB[k] : real.call(this); }});
  }
}
async function state(p) {
  return p.evaluate(() => {
    const buf = term.buffer.active;
    let grok = '';
    for (let y = buf.length - 1; y >= 0 && !grok; y--) {
      const t = buf.getLine(y) ? buf.getLine(y).translateToString(true) : '';
      const m = /size=(\d+)x(\d+)/.exec(t);
      if (m) grok = m[1] + 'x' + m[2];
    }
    const el = document.querySelector('#term .xterm-rows');
    if (!el || !el.children[term.rows - 1]) return {rows: term.rows, cols: term.cols, grok};
    const rows = el.children;
    const first = rows[0].getBoundingClientRect();
    const last = rows[term.rows - 1].getBoundingClientRect();
    const vv = visualViewport;
    return {rows: term.rows, cols: term.cols, grok, firstTop: Math.round(first.top),
      firstVisible: first.top >= vv.offsetTop - 1,
      lastTop: Math.round(last.top), lastBottom: Math.round(last.bottom),
      visTop: Math.round(vv.offsetTop), visBottom: Math.round(vv.offsetTop + vv.height),
      lastVisible: last.top >= vv.offsetTop - 1 && last.bottom <= vv.offsetTop + vv.height + 1};
  });
}
async function settle(p, pred, ms) {
  const end = Date.now() + ms;
  let s = await state(p);
  while (!pred(s) && Date.now() < end) { await wait(100); s = await state(p); }
  return s;
}
const drawn = s => s.grok === s.rows + 'x' + s.cols;
async function device(b, name, kb, pan) {
  const ctx = await b.newContext({...devices[name]});
  const p = await ctx.newPage();
  await p.addInitScript(fakeKeyboard);
  await p.goto(url);
  const closed = await settle(p, drawn, 15000);
  const box = await p.evaluate(() => document.querySelector('#term .xterm-screen').getBoundingClientRect().toJSON());
  await p.touchscreen.tap(box.left + box.width / 2, box.top + box.height / 2);
  await p.evaluate(([kb, pan]) => {
    const h = innerHeight - kb;
    window.KB = {height: h, offsetTop: pan ? innerHeight - h : 0};
    visualViewport.dispatchEvent(new Event('resize'));
    if (pan) visualViewport.dispatchEvent(new Event('scroll'));
  }, [kb, pan]);
  const open = await settle(p, s => drawn(s) && s.rows < closed.rows && s.lastVisible && s.firstVisible, 5000);
  await p.evaluate(() => {
    window.KB = null;
    visualViewport.dispatchEvent(new Event('resize'));
    visualViewport.dispatchEvent(new Event('scroll'));
  });
  const reclosed = await settle(p, s => drawn(s) && s.rows === closed.rows, 5000);
  await ctx.close();
  return {closed, open, reclosed};
}
(async () => {
  const b = await chromium.launch({executablePath: chrome});
  const res = {};
  try {
    res['Pixel 7'] = await device(b, 'Pixel 7', 320, false);
    res['iPhone 14'] = await device(b, 'iPhone 14', 300, true);
  } finally { await b.close(); }
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
        print("note: no node or Chrome")
        return None
    if subprocess.run([node, "-e", "require('playwright')"], capture_output=True, timeout=20).returncode:
        print("note: no playwright module for node (set NODE_PATH)")
        return None
    tmp = tempfile.mkdtemp(prefix="ergo-kb-")
    try:
        home = os.path.join(tmp, "home")
        work = os.path.join(tmp, "work")
        sessions = os.path.join(home, ".grok", "sessions", "kb", SID)
        bindir = os.path.join(tmp, "bin")
        for d in (work, sessions, bindir):
            os.makedirs(d)
        Path(sessions, "summary.json").write_text(json.dumps({
            "info": {"id": SID, "cwd": work}, "generated_title": "keyboard",
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
        srv = subprocess.Popen([sys.executable, "-B", str(ROOT / "ergosphere.py")], cwd=tmp, env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            line = srv.stdout.readline()
            if "ERGOSPHERE_READY" not in line:
                return {"error": "server did not start: %r" % line}
            base = "http://127.0.0.1:%d" % port
            req = urllib.request.Request(base + "/resume", data=json.dumps({"id": SID}).encode(),
                                         headers={"Origin": base, "Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10).read()
            out = subprocess.run([node, "-e", BROWSER_JS, "http://127.0.0.1:%d/chat?resume=%s" % (port, SID), chrome],
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
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check(name, r):
    fails = []
    c, o, z = r["closed"], r["open"], r["reclosed"]
    if c["grok"] != "%sx%s" % (c["rows"], c["cols"]):
        return ["%s: grok never drew at the page size (grok %s, terminal %sx%s)" % (name, c["grok"], c["rows"], c["cols"])]
    if not o["rows"] < c["rows"]:
        fails.append("%s: keyboard open, the terminal kept %s rows (closed %s)" % (name, o["rows"], c["rows"]))
    if o["grok"] != "%sx%s" % (o["rows"], o["cols"]):
        fails.append("%s: keyboard open, grok did not redraw at %sx%s (grok %s)" % (name, o["rows"], o["cols"], o["grok"]))
    if not o["lastVisible"]:
        fails.append("%s: keyboard open, the last row (y %s-%s) is outside the visible area (y %s-%s)" % (
            name, o["lastTop"], o["lastBottom"], o["visTop"], o["visBottom"]))
    if not o["firstVisible"]:
        fails.append("%s: keyboard open, the first row (y %s) is above the visible area (y %s-%s)" % (
            name, o["firstTop"], o["visTop"], o["visBottom"]))
    if z["rows"] != c["rows"] or z["grok"] != c["grok"]:
        fails.append("%s: keyboard closed, %s rows and grok %s, want %s rows (%s)" % (
            name, z["rows"], z["grok"], c["rows"], c["grok"]))
    return fails


def main():
    res = run_browser()
    if res is None:
        print("SKIP keyboard fit: browser check needs node, playwright and Chrome")
        return 0
    print("browser: %s" % json.dumps(res))
    if "error" in res:
        print("FAIL keyboard fit: %s" % res["error"])
        return 1
    fails = []
    for name in ("Pixel 7", "iPhone 14"):
        fails += check(name, res[name])
    for f in fails:
        print("FAIL %s" % f)
    if not fails:
        print("PASS keyboard fit: the last row stays above the keyboard and the rows come back when it closes")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
