"""A pinch zoom of the page outside the terminal leaves grok's size alone (#158).

The session page follows the visual viewport so a phone keyboard does not
hide grok's input line (#119, #137).  A pinch zoom of the page also shrinks
the visual viewport, but by its scale: the terminal must keep its rows, or
grok's terminal, shared by every viewer of the session, is resized.

Pixel 7 profile: wait for grok to draw at the page size, set the page scale
factor to 2 (CDP Emulation.setPageScaleFactor, what a pinch zoom does), read
the rows right after the page has handled the visual viewport's resize
event, then go back to scale 1.  Zoomed and back: same rows, and grok still
at the size it first drew.

Run: python3 -B tests/pinch_zoom_fit_test.py
Needs node's playwright module (NODE_PATH) and Chrome at /usr/bin/google-chrome
(or ERGO_CHROME); skipped without them.  No network, no real grok: the
stand-in TUI is tests/ab/fakegrok.py.  The server setup is keyboard_fit_test's.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import keyboard_fit_test as kf  # noqa: E402

PINCH_JS = r"""
const {chromium, devices} = require('playwright');
const [url, chrome] = process.argv.slice(1);
const wait = ms => new Promise(r => setTimeout(r, ms));
async function state(p) {
  return p.evaluate(() => {
    const buf = term.buffer.active;
    const line = buf.getLine(buf.length - 1);
    const m = /size=(\d+)x(\d+) n=\d+ last=\w+ \$/.exec(line ? line.translateToString(true) : '');
    return {rows: term.rows, cols: term.cols, grok: m ? m[1] + 'x' + m[2] : '',
      scale: visualViewport.scale, vvh: Math.round(visualViewport.height)};
  });
}
async function settle(p, pred, ms) {
  const end = Date.now() + ms;
  let s = await state(p);
  while (!pred(s) && Date.now() < end) { await wait(100); s = await state(p); }
  return s;
}
const drawn = s => s.grok === s.rows + 'x' + s.cols;
// Zoom to `scale` and return the rows as they are once every resize
// listener has run: this one is added after the page's, so it runs last.
async function zoom(p, cdp, scale) {
  await p.evaluate(() => { window.ZR = null; });
  await cdp.send('Emulation.setPageScaleFactor', {pageScaleFactor: scale});
  const end = Date.now() + 30000;
  let zr = null;
  while (Date.now() < end) {
    zr = await p.evaluate(() => window.ZR);
    if (zr && Math.abs(zr.scale - scale) < 0.01) return zr;
    await wait(50);
  }
  return {error: 'no visualViewport resize at scale ' + scale, last: zr};
}
(async () => {
  const b = await chromium.launch({executablePath: chrome});
  const res = {};
  try {
    const ctx = await b.newContext({...devices['Pixel 7']});
    const p = await ctx.newPage();
    await p.goto(url);
    res.before = await settle(p, drawn, 30000);
    await p.evaluate(() => visualViewport.addEventListener('resize', () => {
      window.ZR = {rows: term.rows, cols: term.cols, scale: visualViewport.scale,
        vvh: Math.round(visualViewport.height)};
    }));
    const cdp = await ctx.newCDPSession(p);
    res.zoomed = await zoom(p, cdp, 2);
    res.unzoomed = await zoom(p, cdp, 1);
    res.after = await settle(p, s => drawn(s) && s.rows === res.before.rows, 30000);
    await ctx.close();
  } finally { await b.close(); }
  console.log(JSON.stringify(res));
})().catch(e => { console.error(String(e && e.stack || e)); process.exit(1); });
"""


def check(r):
    b, z, u, a = r["before"], r["zoomed"], r["unzoomed"], r["after"]
    if b["grok"] != "%sx%s" % (b["rows"], b["cols"]):
        return ["grok never drew at the page size (grok %s, terminal %sx%s)" % (b["grok"], b["rows"], b["cols"])]
    fails = []
    for name, s in (("scale 2", z), ("back at scale 1", u)):
        if "error" in s:
            fails.append("%s: %s" % (name, s["error"]))
        elif (s["rows"], s["cols"]) != (b["rows"], b["cols"]):
            fails.append("%s: terminal %sx%s (visual viewport %spx), want %sx%s" % (
                name, s["rows"], s["cols"], s["vvh"], b["rows"], b["cols"]))
    if a["grok"] != b["grok"]:
        fails.append("after the zoom: grok %s, want %s" % (a["grok"], b["grok"]))
    return fails


def main():
    kf.BROWSER_JS = PINCH_JS
    res = kf.run_browser()
    if res is None:
        print("SKIP pinch zoom fit: browser check needs node, playwright and Chrome")
        return 0
    print("browser: %s" % json.dumps(res))
    if "error" in res:
        print("FAIL pinch zoom fit: %s" % res["error"])
        return 1
    fails = check(res)
    for f in fails:
        print("FAIL pinch zoom fit: %s" % f)
    if not fails:
        print("PASS pinch zoom fit: a page zoom keeps the terminal's rows and grok's size")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
