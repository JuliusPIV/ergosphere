"""Dragging grok's own scroll bar (the last column) scrolls the conversation.

Covers (#67): grok draws its scroll bar in the terminal's last column. On
grok's alternate screen a left-button drag that starts there reaches grok as
SGR press, drag motion and release, so grok moves its view and redraws the
thumb: one scroll bar, drawn and positioned by grok. A drag anywhere else
still sends grok nothing (the browser keeps it as a text selection), and on
the normal screen a last-column drag sends nothing either.

Run: NODE_PATH=<node_modules with playwright> python3 -B tests/scroll_bar_test.py
Stdlib only, no network, no real grok: "grok" is tests/ab/fakegrok.py, which
draws a last-column bar and shows "off=<offset>/<max> ... last=<event>".
Skipped without node, playwright or Chrome (/usr/bin/google-chrome or ERGO_CHROME).
"""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN_PORTS = {9241, 9242, 9119, 9120, 9121}

BROWSER_JS = r"""
const {chromium} = require('playwright');
const [url, chrome] = process.argv.slice(1);
const wait = ms => new Promise(r => setTimeout(r, ms));
// grok's status line and the rows where its thumb sits in the last column.
const state = p => p.evaluate(() => {
  const buf = term.buffer.active;
  let text = '';
  const thumb = [];
  for (let y = 0; y < term.rows; y++) {
    const line = buf.getLine(buf.viewportY + y);
    const t = line ? line.translateToString(true) : '';
    if (t.includes('off=')) text = t;
    const c = line && line.getCell(term.cols - 1);
    if (c && c.getChars() === '█') thumb.push(y + 1);
  }
  const m = /off=(\d+)\/(\d+) size=(\d+)x(\d+) n=\d+ last=(\S+)/.exec(text);
  return {off: m ? +m[1] : -1, max: m ? +m[2] : -1, fitted: !!m && +m[3] === term.rows && +m[4] === term.cols,
    last: m ? m[5] : '', thumb, screen: buf.type};
});
async function settle(p, pred, ms) {
  const end = Date.now() + ms;
  let s = await state(p);
  while (!pred(s) && Date.now() < end) { await wait(100); s = await state(p); }
  return s;
}
async function drag(p, x, ya, yb) {
  await p.mouse.move(x, ya);
  await p.mouse.down();
  for (let i = 1; i <= 10; i++) { await p.mouse.move(x, ya + (yb - ya) * i / 10); await wait(30); }
  await p.mouse.up();
}
(async () => {
  const b = await chromium.launch({executablePath: chrome});
  const res = {};
  const p = await (await b.newContext({viewport: {width: 1280, height: 800}})).newPage();
  await p.goto(url);
  res.start = await settle(p, s => s.fitted, 15000);
  const r = await p.evaluate(() => {
    const g = document.querySelector('#term .xterm-screen').getBoundingClientRect();
    return {x: g.left, y: g.top, w: g.width, h: g.height, cw: g.width / term.cols, ch: g.height / term.rows};
  });
  const barX = r.x + r.w - r.cw / 2, top = r.y + r.ch / 2, low = r.y + r.h - r.ch * 1.5;
  // Bar: from the bottom of the track to its top row.
  await drag(p, barX, low, top);
  res.bar = await settle(p, s => s.last === 'release', 3000);
  // Text: a drag in the middle sends grok nothing and selects text.
  await drag(p, r.x + r.w / 2, r.y + r.h / 2, r.y + r.h / 2 + r.ch * 4);
  await wait(300);
  res.text = await state(p);
  res.textSelection = await p.evaluate(() => term.getSelection().length);
  await p.evaluate(() => { window.SENT = []; const s = sock.send.bind(sock);
    sock.send = d => { SENT.push(typeof d === 'string' ? d : new TextDecoder().decode(d)); s(d); }; });
  const sentSince = () => p.evaluate(() => SENT.splice(0).join(''));
  // A release the page never sees (button let go outside the window), then a
  // window blur mid-drag: each ends the drag with a release, and later
  // pointer moves send grok nothing.
  const lost = {
    mouseup: () => p.evaluate(([x, y]) => document.getElementById('term').dispatchEvent(
      new MouseEvent('mousemove', {bubbles: true, clientX: x, clientY: y, buttons: 0})), [barX, r.y + r.h / 2]),
    blur: () => p.evaluate(() => window.dispatchEvent(new Event('blur'))),
  };
  res.lost = {};
  for (const [name, end] of Object.entries(lost)) {
    await p.mouse.move(barX, low);
    await p.mouse.down();
    await p.mouse.move(barX, top, {steps: 5});
    await sentSince();
    await end();
    await p.evaluate(([x, y]) => document.dispatchEvent(
      new MouseEvent('mousemove', {bubbles: true, clientX: x, clientY: y, buttons: 0})), [barX, low]);
    await wait(200);
    res.lost[name] = await sentSince();
    await p.mouse.up();
    await sentSince();
  }
  // Normal screen: leave the alternate screen locally; a bar drag sends nothing.
  await p.evaluate(() => new Promise(done => term.write('\x1b[?1049l', done)));
  await drag(p, barX, low, top);
  await wait(300);
  res.normalSent = await p.evaluate(() => SENT.filter(s => s.includes('\x1b[<')).join(''));
  await b.close();
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
    tmp = tempfile.mkdtemp(prefix="ergo-bar-")
    home = os.path.join(tmp, "home")
    work = os.path.join(home, "proj")
    bindir = os.path.join(tmp, "bin")
    os.makedirs(work)
    os.makedirs(bindir)
    grok = os.path.join(bindir, "grok")
    # argv[0] must be "grok" for the server's /proc check.
    Path(grok).write_text('#!/bin/bash\nexec -a grok %s -B %s "$@"\n' % (
        sys.executable, ROOT / "tests" / "ab" / "fakegrok.py"))
    os.chmod(grok, 0o755)
    port = free_port()
    cfg = os.path.join(tmp, "ergosphere.json")
    Path(cfg).write_text(json.dumps({"bind": "127.0.0.1", "port": port, "agents": {"proj": work}}))
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
        base = "http://127.0.0.1:%d" % port
        req = urllib.request.Request(base + "/launch", data=json.dumps(
            {"agent": "", "cwd": work, "approval": "ask", "touched": []}).encode(),
            headers={"Origin": base, "Content-Type": "application/json"})
        sid = json.load(urllib.request.urlopen(req, timeout=10))["id"]
        out = subprocess.run([node, "-e", BROWSER_JS, base + "/chat?resume=" + sid, chrome],
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
    fails = []
    st, bar, text = res["start"], res["bar"], res["text"]
    if not st["fitted"] or st["off"] != st["max"] or not st["thumb"]:
        return ["grok view not ready at the page size: %r" % st]
    if not (bar["off"] <= st["max"] // 20 and bar["last"] == "release"):
        fails.append("dragging the bar to the top did not scroll grok there: off %d -> %d of %d, last=%s" % (
            st["off"], bar["off"], st["max"], bar["last"]))
    if not (bar["thumb"] and bar["thumb"][0] == 1 and st["thumb"][0] > 1):
        fails.append("grok's thumb did not move to the top: rows %r -> %r" % (st["thumb"], bar["thumb"]))
    if text["off"] != bar["off"] or text["last"] != "release":
        fails.append("a drag over the text reached grok: off %d -> %d, last=%s" % (bar["off"], text["off"], text["last"]))
    if res["textSelection"] <= 0:
        fails.append("a drag over the text selected nothing")
    for name, sent in res["lost"].items():
        if not re.fullmatch(r"\x1b\[<0;\d+;\d+m", sent):
            fails.append("lost %s mid-drag: want one release and then nothing, grok got %r" % (name, sent[:80]))
    if res["normalSent"]:
        fails.append("normal screen: a last-column drag sent mouse reports: %r" % res["normalSent"][:80])
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
