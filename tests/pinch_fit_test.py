"""After a pinch changes the font size, the terminal fills the width (#117).

xterm 5.5 takes the cell width as the row width, rounded to a whole pixel,
over the columns the terminal has.  Right after a font change those are the
old columns, so a single fit could land one column short (Pixel 7: 75 instead
of 76 at 9 px, 35 instead of 36 at 19 px).

Pixel 7 profile, real two-finger touch events on the terminal: one pinch to
8 px, one that spreads step by step to 28 px, one that squeezes back to 8 px.
After each step (read once the page's font size is that step's), the space
left of the terminal's width is under one cell and not below zero.  Then grok
redraws at the terminal's final size.  Only the page's own fit runs: the test
never calls it.

Run: python3 -B tests/pinch_fit_test.py
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
async function until(p, fn, arg) {
  const end = Date.now() + 30000;
  let s = await p.evaluate(fn, arg);
  while (!s.ok && Date.now() < end) { await wait(50); s = await p.evaluate(fn, arg); }
  return s;
}
// grok's status line says the size it drew at; drawn = the terminal's size.
function drawn() {
  const buf = term.buffer.active;
  const line = buf.getLine(buf.length - 1);
  const m = /size=(\d+)x(\d+) n=\d+ last=\w+ \$/.exec(line ? line.translateToString(true) : '');
  const grok = m ? m[1] + 'x' + m[2] : '';
  return {ok: grok === term.rows + 'x' + term.cols, grok, rows: term.rows, cols: term.cols};
}
// The fitted terminal at font size n: the width left over, against one cell.
function fitAt(n) {
  if (term.options.fontSize !== n) return {ok: false, n, font: term.options.fontSize};
  const w = parseInt(getComputedStyle(term.element.parentElement).width);
  const cell = term._core._renderService.dimensions.css.cell.width;
  return {ok: true, n, cols: term.cols, gap: +(w - term.cols * cell).toFixed(2), cell: +cell.toFixed(3)};
}
(async () => {
  const b = await chromium.launch({executablePath: chrome});
  const res = {steps: []};
  try {
    const ctx = await b.newContext({...devices['Pixel 7']});
    const p = await ctx.newPage();
    await p.goto(url);
    res.before = await until(p, drawn);
    const cdp = await ctx.newCDPSession(p);
    const r = await p.evaluate(() => {
      const b = document.querySelector('#term .xterm-screen').getBoundingClientRect();
      return {x: b.left + b.width / 2, y: b.top + b.height / 2};
    });
    const D0 = 100;
    const pts = d => [{x: r.x - d / 2, y: r.y, id: 1}, {x: r.x + d / 2, y: r.y, id: 2}];
    // One pinch from the current size through each size in `sizes`, one
    // move per size: the page sets round(size0 * d / D0).
    async function pinch(sizes) {
      const size0 = await p.evaluate(() => term.options.fontSize);
      await cdp.send('Input.dispatchTouchEvent', {type: 'touchStart', touchPoints: pts(D0)});
      for (const n of sizes) {
        await cdp.send('Input.dispatchTouchEvent', {type: 'touchMove', touchPoints: pts(D0 * n / size0)});
        res.steps.push(await until(p, fitAt, n));
      }
      await cdp.send('Input.dispatchTouchEvent', {type: 'touchEnd', touchPoints: []});
    }
    const up = [], down = [];
    for (let n = 9; n <= 28; n++) up.push(n);
    for (let n = 27; n >= 8; n--) down.push(n);
    await pinch([8]);
    res.steps = [];
    await pinch(up);
    await pinch(down);
    res.after = await until(p, drawn);
    await ctx.close();
  } finally { await b.close(); }
  console.log(JSON.stringify(res));
})().catch(e => { console.error(String(e && e.stack || e)); process.exit(1); });
"""


def check(r):
    if not r["before"]["ok"]:
        return ["grok never drew at the page size (grok %s)" % r["before"]["grok"]]
    fails = []
    if len(r["steps"]) != 40:
        fails.append("%d pinch steps read, want 40" % len(r["steps"]))
    for s in r["steps"]:
        if not s["ok"]:
            fails.append("pinch to %s px: the page's font stayed %s px" % (s["n"], s["font"]))
        elif not 0 <= s["gap"] < s["cell"]:
            fails.append("%s px: %s columns leave %s px of a %s px cell" % (s["n"], s["cols"], s["gap"], s["cell"]))
    if not r["after"]["ok"]:
        fails.append("after the pinch grok drew at %s, terminal %sx%s" % (
            r["after"]["grok"], r["after"]["rows"], r["after"]["cols"]))
    return fails


def main():
    kf.BROWSER_JS = PINCH_JS
    res = kf.run_browser()
    if res is None:
        print("SKIP pinch fit: browser check needs node, playwright and Chrome")
        return 0
    if "error" in res:
        print("FAIL pinch fit: %s" % res["error"])
        return 1
    print("steps: %s" % " ".join("%s:%s" % (s["n"], s.get("cols", "-")) for s in res["steps"]))
    fails = check(res)
    for f in fails:
        print("FAIL pinch fit: %s" % f)
    if not fails:
        print("PASS pinch fit: at every size from 8 to 28 px the terminal leaves under one cell, and grok follows")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
