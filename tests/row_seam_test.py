"""No faint lines between terminal rows at a fractional device scale (#69).

At a device scale that is not a whole number (2.625 on a Pixel, 3.5) xterm's
row height is a fraction of a device pixel, and two rows with the same
background met at a half-covered pixel row: a darker line across the
terminal.  A stand-in grok paints every cell one grey; the test takes a
device-pixel screenshot of the terminal and counts pixel rows where most of
the width is not that grey.  Wanted: none, at scales 2.625 and 3.5 (each at
three font sizes) and at 1, 2 and 3, with the terminal still fitted to the
page (grok drew at the page terminal's size).

Run: python3 -B tests/row_seam_test.py
Needs node's playwright module (NODE_PATH) and Chrome at /usr/bin/google-chrome
(or ERGO_CHROME); skipped without them.  No network, no real grok.
"""
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN_PORTS = {9241, 9242, 9119, 9120, 9121}
# A fresh id per run: Ergosphere finds a session's holder machine-wide via
# /proc, so a fixed id makes concurrent runs on one machine block each other.
SID = str(uuid.uuid4())
# (device scale, font size).  On main every fractional case shows lines.
CASES = [(2.625, 8), (2.625, 13), (2.625, 16), (3.5, 8), (3.5, 13), (3.5, 16),
         (1, 14), (2, 13), (3, 16)]

# A stand-in for grok's TUI: on start and on every resize it paints every
# cell grey (erase with a background colour) and writes "size=RxC $" on the
# last row, so the page can tell that frame is complete and at its size.
FAKE_GROK = r"""
import os, signal, struct, fcntl, termios, tty, select
fd = 0
tty.setraw(fd)
out = os.fdopen(1, "wb", 0)
def draw():
    r, c = struct.unpack("HH", fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 4))
    out.write(b"\x1b[?2026h\x1b[48;2;64;64;64m\x1b[2J\x1b[%d;1Hsize=%dx%d $\x1b[?2026l" % (r, r, c))
wake_r, wake_w = os.pipe()
os.set_blocking(wake_w, False)
signal.set_wakeup_fd(wake_w)
signal.signal(signal.SIGWINCH, lambda *a: None)
out.write(b"\x1b[?1049h\x1b[?25l")
draw()
while True:
    ready, _, _ = select.select([fd, wake_r], [], [])
    if wake_r in ready:
        os.read(wake_r, 512)
        draw()
    if fd in ready:
        d = os.read(fd, 4096)
        if not d or b"\x03" in d:
            break
"""

BROWSER_JS = r"""
const {chromium} = require('playwright');
const [url, chrome, casesJson] = process.argv.slice(1);
const wait = ms => new Promise(r => setTimeout(r, ms));
// grok's frame at the page terminal's size, and the terminal fitted to the page.
const ready = () => {
  if (typeof term === 'undefined' || !term.buffer) return false;
  let want = null;
  try { want = fit.proposeDimensions() || null; } catch (err) {}
  const buf = term.buffer.active, line = buf.getLine(buf.length - 1);
  const m = /size=(\d+)x(\d+) \$/.exec(line ? line.translateToString(true) : '');
  return !!want && !!m && +m[1] === term.rows && +m[2] === term.cols
    && term.cols === want.cols && term.rows === want.rows;
};
async function one(b, dsf, font) {
  const ctx = await b.newContext({viewport: {width: 412, height: 839}, isMobile: true, hasTouch: true, deviceScaleFactor: dsf});
  try {
    await ctx.addInitScript(f => { try { localStorage.setItem('ergosphere.fontSize', String(f)); } catch (e) {} }, font);
    const p = await ctx.newPage();
    await p.goto(url);
    const end = Date.now() + 30000;
    let ok = false;
    while (!(ok = await p.evaluate(ready)) && Date.now() < end) await wait(50);
    if (!ok) return {dsf, font, error: 'grok never drew at the page size'};
    // Everything written is parsed, then two frames: the DOM renderer has drawn it.
    await p.evaluate(() => new Promise(r => term.write('', () => requestAnimationFrame(() => requestAnimationFrame(r)))));
    const g = await p.evaluate(() => {
      const r = document.querySelector('#term .xterm-screen').getBoundingClientRect();
      return {x: r.left, y: r.top, w: r.width, h: r.height, rows: term.rows, cols: term.cols,
        cellH: term._core._renderService.dimensions.css.cell.height};
    });
    const png = await p.screenshot({clip: {x: g.x, y: g.y, width: g.w, height: g.h}});
    const seams = await p.evaluate(async ([b64, last]) => {
      const img = new Image();
      img.src = 'data:image/png;base64,' + b64;
      await img.decode();
      const c = document.createElement('canvas');
      c.width = img.width; c.height = img.height;
      const x = c.getContext('2d');
      x.drawImage(img, 0, 0);
      const d = x.getImageData(0, 0, c.width, c.height).data;
      const out = [];
      // Every pixel row above the last terminal row (it holds the status text).
      for (let y = 0; y < Math.min(c.height, last); y++) {
        let off = 0;
        for (let i = y * c.width * 4, e = i + c.width * 4; i < e; i += 4) {
          if (Math.abs(d[i] - 64) > 2 || Math.abs(d[i + 1] - 64) > 2 || Math.abs(d[i + 2] - 64) > 2) off++;
        }
        if (off > c.width / 2) out.push(y);
      }
      return out;
    }, [png.toString('base64'), Math.floor((g.rows - 1) * g.cellH * dsf)]);
    return {dsf, font, rows: g.rows, cols: g.cols, cellH: +g.cellH.toFixed(3), seams: seams.length, at: seams.slice(0, 5)};
  } finally {
    await ctx.close();
  }
}
(async () => {
  const b = await chromium.launch({executablePath: chrome});
  const res = [];
  try {
    for (const [dsf, font] of JSON.parse(casesJson)) res.push(await one(b, dsf, font));
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
    tmp = tempfile.mkdtemp(prefix="ergo-seam-")
    try:
        home = os.path.join(tmp, "home")
        work = os.path.join(tmp, "work")
        sessions = os.path.join(home, ".grok", "sessions", "seam", SID)
        bindir = os.path.join(tmp, "bin")
        for d in (work, sessions, bindir):
            os.makedirs(d)
        Path(sessions, "summary.json").write_text(json.dumps({
            "info": {"id": SID, "cwd": work}, "generated_title": "row seams",
            "updated_at": "2026-01-01T00:00:00Z"}))
        fake = os.path.join(tmp, "fakegrok.py")
        Path(fake).write_text(FAKE_GROK)
        grok = os.path.join(bindir, "grok")
        # argv[0] must be "grok" for the server's /proc check.
        Path(grok).write_text('#!/bin/bash\nexec -a grok %s -B %s "$@"\n' % (
            shlex.quote(sys.executable), shlex.quote(fake)))
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
            out = subprocess.run([node, "-e", BROWSER_JS, "%s/chat?resume=%s" % (base, SID), chrome, json.dumps(CASES)],
                                 capture_output=True, text=True, timeout=240, env=dict(os.environ, HOME=home))
            try:
                return {"cases": json.loads(out.stdout)}
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


def main():
    res = run_browser()
    if res is None:
        print("SKIP row seams: browser check needs node, playwright and Chrome")
        return 0
    if "error" in res:
        print("FAIL row seams: %s" % res["error"])
        return 1
    fails = 0
    for c in res["cases"]:
        name = "scale %s font %s" % (c["dsf"], c["font"])
        if "error" in c:
            print("FAIL %s: %s" % (name, c["error"]))
            fails += 1
        elif c["seams"]:
            print("FAIL %s: %d pixel rows of lines between terminal rows (first at y %s; %sx%s, cell %spx)" % (
                name, c["seams"], c["at"], c["rows"], c["cols"], c["cellH"]))
            fails += 1
        else:
            print("PASS %s: no lines between rows (%sx%s, cell %spx)" % (name, c["rows"], c["cols"], c["cellH"]))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
