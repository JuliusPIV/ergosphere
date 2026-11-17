// Page checks in headless Chromium: node browser.js <port> <session-id>
// Prints {"browser": version, "checks": {...}} or {"error": why} on stdout.
const {chromium, devices} = require('playwright');
const [port, sid] = process.argv.slice(2);
const url = `http://127.0.0.1:${port}/chat?resume=${sid}`;
const wait = ms => new Promise(r => setTimeout(r, ms));
const STATUS = /off=(\d+)\/(\d+) size=(\d+)x(\d+)/;
const check = (ok, value) => ({ok: !!ok, value: String(value).slice(0, 60)});

// grok's status line as {off, max, rows, cols} plus the page terminal's size.
async function status(p) {
  const r = await p.evaluate(() => {
    if (typeof term === 'undefined' || !term.buffer) return {text: '', rows: 0, cols: 0};
    const buf = term.buffer.active;
    let text = '';
    for (let y = buf.length - 1; y >= 0 && !text; y--) {
      const line = buf.getLine(y);
      const t = line ? line.translateToString(true) : '';
      if (t.includes('off=')) text = t;
    }
    return {text, rows: term.rows, cols: term.cols};
  });
  const m = STATUS.exec(r.text);
  return m ? {off: +m[1], max: +m[2], rows: +m[3], cols: +m[4], termRows: r.rows, termCols: r.cols}
           : {off: NaN, termRows: r.rows, termCols: r.cols};
}
const fitted = s => s.rows === s.termRows && s.cols === s.termCols;

async function settle(p, pred, ms) {
  const end = Date.now() + ms;
  let s = await status(p);
  while (!pred(s) && Date.now() < end) { await wait(100); s = await status(p); }
  return s;
}

async function openPage(ctx) {
  const p = await ctx.newPage();
  await p.goto(url);
  // The pty starts at 80x24; wait until grok has redrawn at the page's size.
  return {p, s: await settle(p, fitted, 15000)};
}

async function screen(p) {
  return p.evaluate(() => {
    const r = document.querySelector('#term .xterm-screen').getBoundingClientRect();
    return {x: r.left, y: r.top, w: r.width, h: r.height};
  });
}

// Headless Chrome has no on-screen keyboard. A phone keyboard shrinks only
// the visual viewport (iOS also pans it), so stand in by overriding
// visualViewport.height/offsetTop and firing its resize and scroll events.
function fakeKeyboard() {
  window.KB = null;
  const P = VisualViewport.prototype;
  for (const k of ['height', 'offsetTop']) {
    const real = Object.getOwnPropertyDescriptor(P, k).get;
    Object.defineProperty(P, k, {configurable: true,
      get() { return window.KB ? window.KB[k] : real.call(this); }});
  }
}
async function setKeyboard(p, kb, pan) {
  await p.evaluate(([kb, pan]) => {
    const h = innerHeight - kb;
    window.KB = kb ? {height: h, offsetTop: pan ? kb : 0} : null;
    visualViewport.dispatchEvent(new Event('resize'));
    visualViewport.dispatchEvent(new Event('scroll'));
  }, [kb, pan]);
}
// The terminal's first and last rows inside the visible area.
async function rowsVisible(p) {
  return p.evaluate(() => {
    const rows = document.querySelector('#term .xterm-rows').children;
    const a = rows[0].getBoundingClientRect(), z = rows[term.rows - 1].getBoundingClientRect();
    const top = visualViewport.offsetTop, bottom = top + visualViewport.height;
    return {ok: a.top >= top - 1 && z.bottom <= bottom + 1,
      text: `rows y${Math.round(a.top)}-${Math.round(z.bottom)} in ${Math.round(top)}-${Math.round(bottom)}`};
  });
}
// Keyboard open: fewer rows, grok redrawn at them, all rows visible; closed:
// the full-height rows again.
async function keyboardCheck(b, device, kb, pan) {
  const ctx = await b.newContext({...devices[device]});
  try {
    const p = await ctx.newPage();
    await p.addInitScript(fakeKeyboard);
    await p.goto(url);
    const s0 = await settle(p, fitted, 15000);
    if (!fitted(s0)) return check(false, 'no grok view at page size');
    const box = await screen(p);
    await p.touchscreen.tap(box.x + box.w / 2, box.y + box.h / 2);
    await setKeyboard(p, kb, pan);
    const s1 = await settle(p, x => fitted(x) && x.rows < s0.rows, 5000);
    const v = await rowsVisible(p);
    await setKeyboard(p, 0, false);
    const s2 = await settle(p, x => fitted(x) && x.rows === s0.rows, 5000);
    const ok = fitted(s1) && s1.rows < s0.rows && v.ok && fitted(s2) && s2.rows === s0.rows;
    return check(ok, `${s0.rows}->${s1.rows}->${s2.rows} ${v.text}`);
  } finally {
    await ctx.close();
  }
}

async function run() {
  const exe = process.env.AB_CHROME || undefined;
  const b = await chromium.launch(exe ? {executablePath: exe} : {});
  const checks = {};
  try {
    let ctx = await b.newContext({viewport: {width: 1280, height: 800}});
    let {p, s} = await openPage(ctx);
    checks.terminal = check(fitted(s), fitted(s) ? `grok drawn at ${s.rows}x${s.cols}` : 'no grok view at page size');
    if (!fitted(s)) return {browser: b.version(), checks};

    const g = await screen(p);
    await p.mouse.move(g.x + g.w / 2, g.y + g.h / 2);
    const before = s.off;
    for (let i = 0; i < 3; i++) { await p.mouse.wheel(0, -100); await wait(80); }
    s = await settle(p, x => x.off !== before, 3000);
    checks.wheel_scroll = check(s.off < before, `3 notches up: moved ${s.off - before}`);

    const r0 = s.rows, c0 = s.cols;
    await p.setViewportSize({width: 900, height: 600});
    s = await settle(p, x => fitted(x) && (x.rows !== r0 || x.cols !== c0), 5000);
    checks.resize = check(fitted(s) && (s.rows !== r0 || s.cols !== c0),
      `${r0}x${c0}->${s.termRows}x${s.termCols} grok ${s.rows}x${s.cols}`);
    await ctx.close();

    ctx = await b.newContext({viewport: {width: 412, height: 839}, hasTouch: true, isMobile: true, deviceScaleFactor: 2});
    ({p, s} = await openPage(ctx));
    const tg = await screen(p);
    const cdp = await ctx.newCDPSession(p);
    const x = tg.x + tg.w / 2, y0 = tg.y + 150, y1 = tg.y + 450;
    const tb = s.off;
    await cdp.send('Input.dispatchTouchEvent', {type: 'touchStart', touchPoints: [{x, y: y0}]});
    for (let i = 1; i <= 15; i++) {
      await cdp.send('Input.dispatchTouchEvent', {type: 'touchMove', touchPoints: [{x, y: y0 + (y1 - y0) * i / 15}]});
      await wait(40);
    }
    await cdp.send('Input.dispatchTouchEvent', {type: 'touchEnd', touchPoints: []});
    s = await settle(p, v => v.off !== tb, 2000);
    checks.touch_scroll = check(s.off < tb, `drag down 300px: moved ${s.off - tb}`);
    await ctx.close();
    checks.keyboard_android = await keyboardCheck(b, 'Pixel 7', 320, false);
    checks.keyboard_ios = await keyboardCheck(b, 'iPhone 14', 300, true);
    return {browser: b.version(), checks};
  } finally {
    await b.close();
  }
}

run().then(r => console.log(JSON.stringify(r)), e => console.log(JSON.stringify({error: String(e && e.message || e).split('\n')[0]})));
