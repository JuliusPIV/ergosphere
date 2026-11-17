// Page checks in headless Chromium: node browser.js <port> <session-id>
// Prints {"browser": version, "checks": {...}} or {"error": why} on stdout.
//
// Every check waits for a state, never for a time: a reading counts only when
// the stand-in's status line on the terminal's last row is complete (it ends
// in " $"; a frame can reach the page in several pieces) and, for scrolling,
// when the stand-in has handled every wheel report the page sent.  The
// deadline only bounds a broken side, so it is long.
const {chromium} = require('playwright');
const [port, sid] = process.argv.slice(2);
const url = `http://127.0.0.1:${port}/chat?resume=${sid}`;
const WAIT = 30000;
const wait = ms => new Promise(r => setTimeout(r, ms));
const STATUS = /off=(\d+)\/(\d+) size=(\d+)x(\d+) n=(\d+) last=(\w+) \$/;
const check = (ok, value) => ({ok: !!ok, value: String(value).slice(0, 60)});

// Counts the SGR wheel reports the page sends to grok, so a check can wait
// until the stand-in has handled all of them.
function countWheelReports() {
  window.abWheelSent = 0;
  const send = WebSocket.prototype.send;
  WebSocket.prototype.send = function (data) {
    if (ArrayBuffer.isView(data) || data instanceof ArrayBuffer) {
      const m = new TextDecoder().decode(data).match(/\x1b\[<6[4-7];/g);
      if (m) window.abWheelSent += m.length;
    }
    return send.call(this, data);
  };
}

// The stand-in's status line on the last row as {off, max, rows, cols, n},
// plus the page terminal's size and the wheel reports the page sent.
async function status(p) {
  const r = await p.evaluate(() => {
    const sent = window.abWheelSent || 0;
    if (typeof term === 'undefined' || !term.buffer) return {text: '', rows: 0, cols: 0, sent};
    const buf = term.buffer.active;
    const line = buf.getLine(buf.length - 1);
    return {text: line ? line.translateToString(true) : '', rows: term.rows, cols: term.cols, sent};
  });
  const m = STATUS.exec(r.text);
  const s = {text: r.text.trim(), termRows: r.rows, termCols: r.cols, sent: r.sent};
  return m ? {...s, ok: true, off: +m[1], max: +m[2], rows: +m[3], cols: +m[4], n: +m[5]} : {...s, ok: false};
}
const fitted = s => s.ok && s.rows === s.termRows && s.cols === s.termCols;
// The stand-in, at the page's size, has handled every wheel report the page
// sent since the reading `from`, and there was at least one.
const caughtUp = from => s => fitted(s) && s.sent > from.sent && s.n - from.n === s.sent - from.sent;

// Polls until pred holds or WAIT runs out; returns the last reading and
// whether pred held, so a failed check can say what it saw.
async function until(p, pred) {
  const end = Date.now() + WAIT;
  let s = await status(p);
  while (!pred(s) && Date.now() < end) { await wait(50); s = await status(p); }
  return {s, met: pred(s)};
}
const seen = s => `saw "${s.text.slice(0, 30)}" page ${s.termRows}x${s.termCols} sent ${s.sent}`;

async function openPage(ctx) {
  await ctx.addInitScript(countWheelReports);
  const p = await ctx.newPage();
  p.setDefaultTimeout(WAIT);
  await p.goto(url);
  // The pty starts at 80x24; wait until grok has redrawn at the page's size.
  return {p, ...(await until(p, fitted))};
}

async function screen(p) {
  return p.evaluate(() => {
    const r = document.querySelector('#term .xterm-screen').getBoundingClientRect();
    return {x: r.left, y: r.top, w: r.width, h: r.height};
  });
}

async function run() {
  const exe = process.env.AB_CHROME || undefined;
  const b = await chromium.launch(exe ? {executablePath: exe} : {});
  const checks = {};
  try {
    let ctx = await b.newContext({viewport: {width: 1280, height: 800}});
    let {p, s, met} = await openPage(ctx);
    checks.terminal = check(met, met ? `grok drawn at ${s.rows}x${s.cols}` : `no grok view at page size: ${seen(s)}`);
    if (!met) return {browser: b.version(), checks};

    const g = await screen(p);
    await p.mouse.move(g.x + g.w / 2, g.y + g.h / 2);
    const before = s;
    for (let i = 0; i < 3; i++) await p.mouse.wheel(0, -100);
    ({s, met} = await until(p, caughtUp(before)));
    checks.wheel_scroll = check(met && s.off < before.off,
      met ? `3 notches up: moved ${s.off - before.off}` : `not handled: ${seen(s)}`);

    const r0 = s.rows, c0 = s.cols;
    await p.setViewportSize({width: 900, height: 600});
    ({s, met} = await until(p, x => fitted(x) && (x.rows !== r0 || x.cols !== c0)));
    checks.resize = check(met, met ? `${r0}x${c0}->${s.termRows}x${s.termCols} grok ${s.rows}x${s.cols}`
                                   : `${r0}x${c0}-> ${seen(s)}`);
    await ctx.close();

    ctx = await b.newContext({viewport: {width: 412, height: 839}, hasTouch: true, isMobile: true, deviceScaleFactor: 2});
    ({p, s, met} = await openPage(ctx));
    if (!met) {
      checks.touch_scroll = check(false, `no grok view on the phone page: ${seen(s)}`);
      return {browser: b.version(), checks};
    }
    const tg = await screen(p);
    const cdp = await ctx.newCDPSession(p);
    const x = tg.x + tg.w / 2, y0 = tg.y + 150, y1 = tg.y + 450;
    const tb = s;
    await cdp.send('Input.dispatchTouchEvent', {type: 'touchStart', touchPoints: [{x, y: y0}]});
    for (let i = 1; i <= 15; i++) {
      await cdp.send('Input.dispatchTouchEvent', {type: 'touchMove', touchPoints: [{x, y: y0 + (y1 - y0) * i / 15}]});
      await wait(40);
    }
    // Hold still before lifting.  The page flings only on a lift within 80 ms
    // of the last move, and a fling's length depends on frame timing, so it
    // would differ from run to run.  A longer hold under load changes nothing.
    await wait(150);
    await cdp.send('Input.dispatchTouchEvent', {type: 'touchEnd', touchPoints: []});
    ({s, met} = await until(p, caughtUp(tb)));
    checks.touch_scroll = check(met && s.off < tb.off,
      met ? `drag down 300px: moved ${s.off - tb.off}` : `not handled: ${seen(s)}`);
    await ctx.close();
    return {browser: b.version(), checks};
  } finally {
    await b.close();
  }
}

run().then(r => console.log(JSON.stringify(r)), e => console.log(JSON.stringify({error: String(e && e.message || e).split('\n')[0]})));
