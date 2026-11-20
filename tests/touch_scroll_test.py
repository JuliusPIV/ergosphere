"""The session page shows no dead scroll bar and scrolls grok by touch.

Covers (#66): no visible xterm or page scroll bar; the fit addon gives the
hidden bar's width back to the terminal columns; the page script parses; in
a browser, a one-finger drag in Pixel 7 emulation sends SGR wheel reports to
grok, a tap still focuses the terminal, and the terminal fills the width.
Normal-screen output taller than the terminal stays in xterm's scrollback,
reachable by the wheel and by a touch drag.
Pinch (#86): two fingers change the terminal font size (clamped 8-28 px) and
refit, so grok gets a resize; the page itself never zooms; the size is kept
per browser and used on the next page load.

Run: python3 -B tests/touch_scroll_test.py
Stdlib only, no network, no real grok: "grok" is a small python TUI.
The node checks are skipped without node. The browser check needs node's
playwright module (set NODE_PATH to the node_modules holding it) and Chrome
at /usr/bin/google-chrome (or ERGO_CHROME); it is skipped without them.
"""
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN_PORTS = {9241, 9242, 9119, 9120, 9121}


def load():
    spec = importlib.util.spec_from_file_location("ergosphere", ROOT / "ergosphere.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def page_script(page):
    # The one inline <script>; the others are /vendor/*.js by src.
    m = re.search(r"<script>\n(.*?)</script>", page, re.S)
    return m.group(1) if m else ""


def check_css(page):
    fails = []
    css = page[page.index("<style>"):page.index("</style>")]
    if not re.search(r"#term \.xterm-viewport \{[^}]*overflow-y: hidden", css):
        fails.append("xterm's viewport keeps its scroll bar (no overflow-y: hidden)")
    if not re.search(r"html, body \{[^}]*overflow: hidden;[^}]*overscroll-behavior: none", css):
        fails.append("the page itself can scroll (html, body lack overflow: hidden; overscroll-behavior: none)")
    if "100dvh" not in css or "100vh" not in css:
        fails.append("#wrap height needs 100dvh with the 100vh fallback")
    if not re.search(r"#term \{[^}]*touch-action: none;", css):
        fails.append("#term needs touch-action: none (no browser pan or zoom; drag and pinch are the script's)")
    return fails


def check_node_syntax(script):
    node = shutil.which("node")
    if not node:
        print("note: no node, page script syntax check skipped")
        return []
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(script)
    try:
        out = subprocess.run([node, "--check", f.name], capture_output=True, text=True, timeout=20)
    finally:
        os.unlink(f.name)
    if out.returncode:
        return ["page script does not parse: %s" % out.stderr.strip()[-300:]]
    return []


GUTTER_JS = r"""
// The vendored fit addon against a terminal built with the page's options,
// after the page's own fix-up once the terminal is open. xterm 5.5 sets
// viewport.scrollBarWidth to "viewport.offsetWidth - scrollArea.offsetWidth
// || 15": a hidden bar takes no width, so it is 15. The addon subtracts it
// unless scrollback is 0.
var window = {getComputedStyle: function (el) { return {getPropertyValue: function (k) {
  return el.style[k] || "0"; }}; }};
globalThis.window = window;
globalThis.self = globalThis;
var FitAddon = require("./vendor/addon-fit.js");
// The page reads its saved pinch size from localStorage; 14 is the default.
function savedFontSize() { return 14; }
var opts = (%s);
var parent = {style: {width: "800px", height: "340px"}};
var term = {options: opts, rows: 24, cols: 80,
  element: {parentElement: parent, style: {}},
  _core: {viewport: {scrollBarWidth: 15},
          _renderService: {dimensions: {css: {cell: {width: 8, height: 17}}}}}};
%s
if (typeof dropScrollBarGutter === "function") dropScrollBarGutter();
var fit = new FitAddon.FitAddon();
fit.activate(term);
console.log(JSON.stringify(fit.proposeDimensions()));
"""


def check_gutter(script):
    node = shutil.which("node")
    if not node:
        print("note: no node, fit gutter check skipped")
        return []
    m = re.search(r"new Terminal\((\{.*?\})\);", script)
    if not m:
        return ["no new Terminal({...}) in the page script"]
    f = re.search(r"^function dropScrollBarGutter\(\) \{\n.*?^\}\n", script, re.S | re.M)
    out = subprocess.run([node, "-e", GUTTER_JS % (m.group(1), f.group(0) if f else "")], cwd=str(ROOT),
                         capture_output=True, text=True, timeout=20)
    try:
        dims = json.loads(out.stdout)
    except ValueError:
        return ["fit gutter: node failed: %s" % out.stderr.strip()[-300:]]
    if dims.get("cols") != 100:
        return ["fit reserves a scroll bar gutter: 800px of 8px cells gave %s cols, not 100" % dims.get("cols")]
    return []


# A stand-in for grok's TUI: the alternate screen and SGR mouse modes grok
# sets, 40 numbered rows, and every input byte logged as hex lines.
FAKE_GROK = r"""
import os, sys, tty
fd = sys.stdin.fileno()
tty.setraw(fd)
log = open(os.environ["FAKEGROK_LOG"], "a")
out = sys.stdout.buffer
out.write(b"\x1b[?1049h\x1b[?25l\x1b[?1000h\x1b[?1002h\x1b[?1006h\x1b[H")
out.write(b"".join(b"row %02d\r\n" % i for i in range(40)))
out.flush()
while True:
    d = os.read(fd, 4096)
    if not d or b"\x03" in d:
        break
    log.write(d.hex() + "\n")
    log.flush()
"""

BROWSER_JS = r"""
const {chromium, devices} = require('playwright');
const [url, chrome] = process.argv.slice(1);
const wait = ms => new Promise(r => setTimeout(r, ms));
// Settle steps wait for a page condition with a deadline, never a fixed
// sleep: on a loaded machine a fixed sleep can end before the page fits or
// the fling stops (#187, #190). The short sleeps left pace the input events.
const WAIT = 15000;
async function until(p, fn, arg) {
  const end = Date.now() + WAIT;
  let ok;
  while (!(ok = await p.evaluate(fn, arg)) && Date.now() < end) await wait(50);
  return !!ok;
}
const fitted = () => {
  let want = null;
  try { want = fit.proposeDimensions() || null; } catch (err) {}
  return !!want && term.cols === want.cols && term.rows === want.rows;
};
// Loaded: grok's socket is open, its alternate screen is drawn and the
// terminal is fitted to the page (not xterm's default 80x24).
const loaded = p => until(p, () => {
  let want = null;
  try { want = fit.proposeDimensions() || null; } catch (err) {}
  return !!sock && sock.readyState === 1 && term.buffer.active.type === 'alternate'
    && !!want && term.cols === want.cols && term.rows === want.rows;
});
const flingStopped = p => until(p, () => window.fling == null);
async function measure(p) {
  return p.evaluate(() => {
    const se = document.scrollingElement;
    const vp = document.querySelector('#term .xterm-viewport');
    const sc = document.querySelector('#term .xterm-screen').getBoundingClientRect();
    const box = document.getElementById('term').getBoundingClientRect();
    return {pageScroll: se.scrollHeight - se.clientHeight,
      vpOverflowY: getComputedStyle(vp).overflowY, vpBarPx: vp.offsetWidth - vp.clientWidth,
      gapPx: box.width - sc.width, cellPx: sc.width / term.cols, cols: term.cols};
  });
}
// Leave grok's alternate screen locally and write 200 lines, more than the
// terminal is tall, as output on the normal screen.
async function normalScreen(p) {
  return p.evaluate(() => new Promise(done => {
    let s = '\x1b[?1049l\x1b[2J\x1b[H';
    for (let i = 0; i < 200; i++) s += 'normal ' + i + '\r\n';
    term.write(s, () => done({len: term.buffer.normal.length, rows: term.rows,
      top: term.buffer.active.viewportY, base: term.buffer.active.baseY}));
  }));
}
const viewTop = p => p.evaluate(() => term.buffer.active.viewportY);
// One finger: down, a single move event, up at once; then wait until the
// fling has stopped. Returns what the move alone sent and whether it stopped.
async function oneMove(p, cdp, x, ya, yb) {
  await cdp.send('Input.dispatchTouchEvent', {type: 'touchStart', touchPoints: [{x, y: ya}]});
  await wait(30);
  await cdp.send('Input.dispatchTouchEvent', {type: 'touchMove', touchPoints: [{x, y: yb}]});
  const moveSent = await p.evaluate(() => SENT.join(''));
  await cdp.send('Input.dispatchTouchEvent', {type: 'touchEnd', touchPoints: []});
  return {moveSent, settled: await flingStopped(p)};
}
(async () => {
  const b = await chromium.launch({executablePath: chrome, ignoreDefaultArgs: ['--hide-scrollbars']});
  const res = {loaded: {}};
  let ctx = await b.newContext({viewport: {width: 1280, height: 800}});
  let p = await ctx.newPage();
  await p.goto(url); res.loaded.desktop = await loaded(p);
  res.desktop = await measure(p);
  res.desktop.normal = await normalScreen(p);
  const d = await p.evaluate(() => document.querySelector('#term .xterm-screen').getBoundingClientRect().toJSON());
  await p.mouse.move(d.left + d.width / 2, d.top + d.height / 2);
  for (let i = 0; i < 3; i++) { await p.mouse.wheel(0, -200); await wait(80); }
  await until(p, base => term.buffer.active.viewportY < base, res.desktop.normal.base);
  res.desktop.normalTopAfterWheel = await viewTop(p);
  await ctx.close();
  ctx = await b.newContext({...devices['Pixel 7']});
  p = await ctx.newPage();
  let cdp = await ctx.newCDPSession(p);
  await p.goto(url); res.loaded.mobile = await loaded(p);
  res.mobile = await measure(p);
  await p.evaluate(() => { window.SENT = []; const s = sock.send.bind(sock);
    sock.send = d => { SENT.push(typeof d === 'string' ? d : new TextDecoder().decode(d)); s(d); }; });
  const r = await p.evaluate(() => document.querySelector('#term .xterm-screen').getBoundingClientRect().toJSON());
  const x = r.left + r.width / 2, y0 = r.top + 150;
  // Slow one-finger drag down 300px: no fling, older lines (wheel up, 64).
  await cdp.send('Input.dispatchTouchEvent', {type: 'touchStart', touchPoints: [{x, y: y0}]});
  for (let i = 1; i <= 15; i++) {
    await cdp.send('Input.dispatchTouchEvent', {type: 'touchMove', touchPoints: [{x, y: y0 + 20 * i}]});
    await wait(40);
  }
  await wait(150);
  await cdp.send('Input.dispatchTouchEvent', {type: 'touchEnd', touchPoints: []});
  await flingStopped(p);
  res.dragSent = await p.evaluate(() => SENT.join(''));
  res.scrollY = await p.evaluate(() => scrollY);
  // A flick delivered as one 200px move: it scrolls, and the fling goes on.
  await p.evaluate(() => { SENT.length = 0; });
  res.oneMove = await oneMove(p, cdp, x, y0, y0 + 200);
  res.oneMoveSent = await p.evaluate(() => SENT.join(''));
  // Two fingers: no wheel report and no preventDefault (left for pinch).
  // Count preventDefault itself: xterm's cancel also stops propagation, so a
  // listener on window would never see a prevented move.
  await p.evaluate(() => { SENT.length = 0; window.PREVENTED = 0;
    const o = Event.prototype.preventDefault;
    Event.prototype.preventDefault = function () {
      if (this.type === 'touchmove' && this.touches && this.touches.length > 1) PREVENTED++;
      return o.call(this); }; });
  await cdp.send('Input.dispatchTouchEvent', {type: 'touchStart', touchPoints: [{x: x - 40, y: y0, id: 1}, {x: x + 40, y: y0, id: 2}]});
  for (let i = 1; i <= 10; i++) {
    await cdp.send('Input.dispatchTouchEvent', {type: 'touchMove', touchPoints: [{x: x - 40, y: y0 + 20 * i, id: 1}, {x: x + 40, y: y0 + 20 * i, id: 2}]});
    await wait(30);
  }
  await cdp.send('Input.dispatchTouchEvent', {type: 'touchEnd', touchPoints: []});
  await flingStopped(p);
  res.twoSent = await p.evaluate(() => SENT.join(''));
  res.twoPrevented = await p.evaluate(() => PREVENTED);
  // A tap focuses the terminal and sends no wheel report.
  await p.evaluate(() => { SENT.length = 0; document.activeElement && document.activeElement.blur(); });
  await p.touchscreen.tap(x, r.top + 200);
  res.tapFocused = await until(p, () => !!document.activeElement && document.activeElement.classList.contains('xterm-helper-textarea'));
  await flingStopped(p);
  res.tapSent = await p.evaluate(() => SENT.join(''));
  // On the normal screen a drag scrolls xterm's own scrollback.
  res.mobile.normal = await normalScreen(p);
  await cdp.send('Input.dispatchTouchEvent', {type: 'touchStart', touchPoints: [{x, y: y0}]});
  for (let i = 1; i <= 15; i++) {
    await cdp.send('Input.dispatchTouchEvent', {type: 'touchMove', touchPoints: [{x, y: y0 + 20 * i}]});
    await wait(40);
  }
  await wait(150);
  await cdp.send('Input.dispatchTouchEvent', {type: 'touchEnd', touchPoints: []});
  await flingStopped(p);
  res.mobile.normalTopAfterDrag = await viewTop(p);
  res.mobile.normalOneMove = await oneMove(p, cdp, x, y0, y0 + 200);
  res.mobile.normalTopAfterOneMove = await viewTop(p);
  await ctx.close();
  // Pinch in a fresh page: two fingers change the font size and refit, grok
  // gets the new size, the page never zooms. Fresh, because on the normal
  // screen with scrollback xterm's own touch handler cancels the move.
  ctx = await b.newContext({...devices['Pixel 7']});
  p = await ctx.newPage();
  cdp = await ctx.newCDPSession(p);
  await p.goto(url); res.loaded.pinch = await loaded(p);
  await p.evaluate(() => { window.SENT = []; const s = sock.send.bind(sock);
    sock.send = d => { SENT.push(typeof d === 'string' ? d : new TextDecoder().decode(d)); s(d); }; });
  const fontState = () => p.evaluate(() => ({font: term.options.fontSize, cols: term.cols, rows: term.rows,
    scale: visualViewport.scale}));
  // Real two-finger touch events: Input.synthesizePinchGesture zooms
  // nothing in this headless Chrome, even with touch-action auto.
  const cy = r.top + r.height / 2;
  async function pinch(d0, d1) {
    const pts = d => [{x: x - d / 2, y: cy, id: 1}, {x: x + d / 2, y: cy, id: 2}];
    await cdp.send('Input.dispatchTouchEvent', {type: 'touchStart', touchPoints: pts(d0)});
    for (let i = 1; i <= 15; i++) {
      await cdp.send('Input.dispatchTouchEvent', {type: 'touchMove', touchPoints: pts(d0 + (d1 - d0) * i / 15)});
      await wait(30);
    }
    await cdp.send('Input.dispatchTouchEvent', {type: 'touchEnd', touchPoints: []});
    await until(p, fitted);
  }
  res.pinch = {start: await fontState()};
  await pinch(100, 150);
  res.pinch.spread = await fontState();
  await pinch(150, 50);
  res.pinch.squeeze = await fontState();
  await pinch(50, 400);
  res.pinch.max = await fontState();
  await pinch(400, 20);
  res.pinch.min = await fontState();
  res.pinchSent = await p.evaluate(() => SENT.join(''));
  res.pinch.resizes = await p.evaluate(() => SENT.filter(s => s.startsWith('{"type":"resize"')).length);
  await p.reload(); res.loaded.reload = await loaded(p);
  res.pinch.reload = await fontState();
  await ctx.close();
  await b.close();
  console.log(JSON.stringify(res));
})().catch(e => { console.error(String(e && e.stack || e)); process.exit(1); });
"""


BROWSER_WAIT_S = 15  # WAIT in BROWSER_JS, in seconds


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


def check_pinch(pn):
    fails = []
    st, sp, sq, mx, mn = pn["start"], pn["spread"], pn["squeeze"], pn["max"], pn["min"]
    for name in ("spread", "squeeze", "max", "min", "reload"):
        if pn[name]["scale"] != 1:
            fails.append("pinch: the page zoomed (%s: scale %s)" % (name, pn[name]["scale"]))
    if not (sp["font"] > st["font"] and sp["cols"] < st["cols"]):
        fails.append("pinch: spreading did not enlarge the text (font %s->%s, cols %s->%s)" % (
            st["font"], sp["font"], st["cols"], sp["cols"]))
    if not (sq["font"] < sp["font"] and sq["cols"] > sp["cols"]):
        fails.append("pinch: squeezing did not shrink the text (font %s->%s, cols %s->%s)" % (
            sp["font"], sq["font"], sp["cols"], sq["cols"]))
    if mx["font"] != 28 or mn["font"] != 8:
        fails.append("pinch: font size not clamped to 8-28 (max %s, min %s)" % (mx["font"], mn["font"]))
    if pn["resizes"] < 4:
        fails.append("pinch: grok got %d resize messages for 4 pinches" % pn["resizes"])
    if pn["reload"]["font"] != mn["font"]:
        fails.append("pinch: the size is not kept on reload (%s, was %s)" % (pn["reload"]["font"], mn["font"]))
    return fails


def check_browser():
    node = shutil.which("node")
    chrome = os.environ.get("ERGO_CHROME", "/usr/bin/google-chrome")
    if not node or not os.path.exists(chrome):
        print("note: no node or Chrome, browser check skipped")
        return None
    probe = subprocess.run([node, "-e", "require('playwright')"], capture_output=True, timeout=20)
    if probe.returncode:
        print("note: no playwright module for node, browser check skipped")
        return None
    tmp = tempfile.mkdtemp(prefix="ergo-touch-")
    home = os.path.join(tmp, "home")
    work = os.path.join(home, "proj")
    bindir = os.path.join(tmp, "bin")
    os.makedirs(work)
    os.makedirs(bindir)
    fake = os.path.join(tmp, "fakegrok.py")
    log = os.path.join(tmp, "grok.log")
    Path(fake).write_text(FAKE_GROK)
    grok = os.path.join(bindir, "grok")
    # argv[0] must be "grok" for the server's /proc check.
    Path(grok).write_text('#!/bin/bash\nexec -a grok %s %s "$@"\n' % (sys.executable, fake))
    os.chmod(grok, 0o755)
    port = free_port()
    cfg = os.path.join(tmp, "ergosphere.json")
    Path(cfg).write_text(json.dumps({"bind": "127.0.0.1", "port": port, "agents": {"proj": work}}))
    env = dict(os.environ, HOME=home, ERGOSPHERE_CONFIG=cfg, FAKEGROK_LOG=log,
               PATH=bindir + os.pathsep + os.environ.get("PATH", ""))
    env.pop("ERGOSPHERE_TOKEN", None)
    env.pop("ERGOSPHERE_PORT", None)
    srv = subprocess.Popen([sys.executable, "-B", str(ROOT / "ergosphere.py")], env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        line = srv.stdout.readline()
        if "ERGOSPHERE_READY" not in line:
            return ["browser: server did not start: %r" % line]
        base = "http://127.0.0.1:%d" % port
        req = urllib.request.Request(base + "/launch", data=json.dumps(
            {"agent": "", "cwd": work, "approval": "ask", "touched": []}).encode(),
            headers={"Origin": base, "Content-Type": "application/json"})
        sid = json.load(urllib.request.urlopen(req, timeout=10))["id"]
        out = subprocess.run([node, "-e", BROWSER_JS, base + "/chat?resume=" + sid, chrome],
                             capture_output=True, text=True, timeout=120, env=dict(os.environ, HOME=home))
        try:
            res = json.loads(out.stdout)
        except ValueError:
            return ["browser: node failed: %s" % out.stderr.strip()[-500:]]
    finally:
        srv.terminate()
        try:
            srv.wait(10)
        except subprocess.TimeoutExpired:
            srv.kill()
        shutil.rmtree(tmp, ignore_errors=True)
    print("browser: %s" % json.dumps(res))
    fails = []
    for name, ok in sorted(res["loaded"].items()):
        if not ok:
            fails.append("%s: the page did not load within %ds (socket open, alternate screen, terminal fitted)"
                         % (name, BROWSER_WAIT_S))
    for name in ("desktop", "mobile"):
        m = res[name]
        if m["vpBarPx"] > 0 or m["vpOverflowY"] != "hidden":
            fails.append("%s: xterm scroll bar visible (%spx, overflow-y %s)" % (name, m["vpBarPx"], m["vpOverflowY"]))
        if m["pageScroll"] > 0:
            fails.append("%s: the page scrolls by %spx" % (name, m["pageScroll"]))
        if m["gapPx"] >= m["cellPx"]:
            fails.append("%s: %.1fpx right of the last column is unused (cell %.1fpx)" % (name, m["gapPx"], m["cellPx"]))
    ups = re.findall(r"\x1b\[<64;\d+;\d+M", res["dragSent"])
    if len(ups) < 3:
        fails.append("touch drag sent %d wheel-up reports, want several: %r" % (len(ups), res["dragSent"][:80]))
    if re.search(r"\x1b\[<65;", res["dragSent"]):
        fails.append("a downward drag sent wheel-down reports")
    if res["scrollY"]:
        fails.append("the touch drag scrolled the page by %spx" % res["scrollY"])
    if "\x1b[<6" in res["twoSent"]:
        fails.append("a two-finger drag sent wheel reports")
    if res["twoPrevented"]:
        fails.append("a two-finger drag was preventDefault-ed %d times" % res["twoPrevented"])
    if not res["tapFocused"]:
        fails.append("a tap did not focus the terminal")
    for name in ("desktop", "mobile"):
        n = res[name]["normal"]
        if n["len"] < 200:
            fails.append("%s: normal-screen output beyond the terminal height is lost: %d of 200+ lines kept" % (name, n["len"]))
    n = res["desktop"]["normal"]
    if not res["desktop"]["normalTopAfterWheel"] < n["base"]:
        fails.append("desktop: the wheel does not scroll the normal screen's history (top %s, base %s)" % (res["desktop"]["normalTopAfterWheel"], n["base"]))
    n = res["mobile"]["normal"]
    if not res["mobile"]["normalTopAfterDrag"] < n["base"]:
        fails.append("mobile: a drag does not scroll the normal screen's history (top %s, base %s)" % (res["mobile"]["normalTopAfterDrag"], n["base"]))
    for name, om in (("alternate", res["oneMove"]), ("normal", res["mobile"]["normalOneMove"])):
        if not om["settled"]:
            fails.append("%s screen: the fling after a 200px flick did not stop within %ds" % (name, BROWSER_WAIT_S))
    # The move alone sends a few reports; the fling must clearly add to them.
    at_move = len(re.findall(r"\x1b\[<64;\d+;\d+M", res["oneMove"]["moveSent"]))
    ones = re.findall(r"\x1b\[<64;\d+;\d+M", res["oneMoveSent"])
    if not at_move or len(ones) < 2 * at_move:
        fails.append("a 200px flick in one move event sent %d wheel-up reports, %d from the move alone; "
                     "want the move plus a fling of at least as many again" % (len(ones), at_move))
    if not res["mobile"]["normalTopAfterOneMove"] < res["mobile"]["normalTopAfterDrag"]:
        fails.append("normal screen: a 200px flick in one move event did not scroll (top %s, was %s)" % (
            res["mobile"]["normalTopAfterOneMove"], res["mobile"]["normalTopAfterDrag"]))
    fails += check_pinch(res["pinch"])
    if "\x1b[<6" in res["pinchSent"]:
        fails.append("a pinch sent wheel reports")
    if "\x1b[<6" in res["tapSent"]:
        fails.append("a tap sent a wheel report")
    return fails


def main():
    erg = load()
    page = erg.chat_page("", None, True, "")
    script = page_script(page)
    fails = []
    for name, got in (("css", check_css(page)), ("syntax", check_node_syntax(script)),
                      ("gutter", check_gutter(script)), ("browser", check_browser())):
        if got is None:
            print("SKIP %s" % name)
        elif got:
            for f in got:
                print("FAIL %s: %s" % (name, f))
            fails += got
        else:
            print("PASS %s" % name)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
