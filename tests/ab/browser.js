// Page checks in headless Chromium: node browser.js <port> <session-id>
// Prints {"browser": version, "checks": {...}} or {"error": why} on stdout.
//
// Every check waits for a state, never for a time: a reading counts only when
// the stand-in's status line on the terminal's last row is complete (it ends
// in " $"; a frame can reach the page in several pieces) and, for scrolling,
// when the stand-in has handled every wheel report the page sent.  The
// deadline only bounds a broken side, so it is long.
const {chromium, devices} = require('playwright');
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

// The stand-in's status line on the last row as {off, max, rows, cols, n, last},
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
  return m ? {...s, ok: true, off: +m[1], max: +m[2], rows: +m[3], cols: +m[4], n: +m[5], last: m[6]} : {...s, ok: false};
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

// Records each window.open (#34).  The page must never call it: only the
// user's click on the bar link opens a tab.  The real open still runs.
function recordOpens() {
  window.abOpens = [];
  const open = window.open;
  window.open = function (u) {
    window.abOpens.push(String(u));
    return open.apply(this, arguments);
  };
}

// The page's bar link and the window.open calls it made, and how many pages
// the browser has (a tab opened by any means adds one).
async function barState(ctx, p) {
  const r = await p.evaluate(() => {
    const a = document.querySelector('#link a');
    return {href: a ? a.href : '', target: a ? a.target : '', rel: a ? a.rel : '',
      links: document.querySelectorAll('#link a').length, opens: window.abOpens.slice()};
  });
  return {...r, pages: ctx.pages().length};
}

async function untilBar(ctx, p, pred) {
  const end = Date.now() + WAIT;
  let r = await barState(ctx, p);
  while (!pred(r) && Date.now() < end) { await wait(50); r = await barState(ctx, p); }
  return r;
}

// A click on grok's link (the stand-in's row 1, column 1) puts that link in
// the bar and opens no tab.  The page refuses javascript:, file: and its own
// origin.  A click on the bar link opens exactly that URL, with no opener.
// The server takes one link per 0.5 s per session (LINK_PAUSE): the next
// check asks for its link only once that has passed since the last one.
let lastLinkAt = 0;
const pastPause = () => wait(Math.max(0, lastLinkAt + 700 - Date.now()));
// After the bar link click the focus is on that link: keys go to grok again
// only once the terminal has it.
const focusTerm = p => p.evaluate(() => term.focus());

async function linkCheck(ctx, p) {
  const g = await screen(p);
  const pages = ctx.pages().length;
  await p.mouse.click(g.x + 2, g.y + 2);
  const {s, met} = await until(p, x => x.ok && /^(linkRc\d+|noDisplay)$/.test(x.last));
  if (!met) return check(false, `click not handled: ${seen(s)}`);
  lastLinkAt = Date.now();
  // The helper's open frame is queued before the server answers the helper,
  // so it is on the socket before the redraw that shows linkRc0.
  let r = await barState(ctx, p);
  if (r.opens.length || r.pages !== pages) return check(false, `${s.last}: page opened a tab itself (${r.opens[0] || 'new page'})`);
  if (r.href !== 'https://example.com/ab-link' || r.target !== '_blank' || r.rel !== 'noopener noreferrer') {
    return check(false, `${s.last}: bar link ${r.href.slice(0, 30) || 'none'} target=${r.target} rel=${r.rel}`);
  }
  await p.evaluate(() => {
    openLink('javascript:alert(1)'); openLink('file:///etc/passwd'); openLink(location.origin + '/models');
  });
  r = await barState(ctx, p);
  if (r.href !== 'https://example.com/ab-link' || r.links !== 1 || r.opens.length) {
    return check(false, `bad link taken: bar ${r.href.slice(0, 30)} links=${r.links}`);
  }
  // The new tab gets a stand-in page: the A/B sandbox has no network.
  await ctx.route('https://example.com/**', route => route.fulfill({contentType: 'text/html', body: '<p>ab</p>'}));
  const [tab] = await Promise.all([ctx.waitForEvent('page', {timeout: WAIT}), p.click('#link a')]);
  await tab.waitForLoadState('domcontentloaded');
  const opener = await tab.evaluate(() => window.opener === null ? 'null' : 'set');
  const got = tab.url();
  await tab.close();
  return check(got === 'https://example.com/ab-link' && opener === 'null',
    `${s.last} bar link; js/file/self refused; click opened ${got.slice(8, 30)} opener ${opener}`);
}

// Any process under grok can ask for a link.  The stand-in asks for one when
// it reads "k", as a model's shell tool could right after the user typed: a
// bar link, no tab.
async function keyCheck(ctx, p) {
  const pages = ctx.pages().length;
  await pastPause();
  await focusTerm(p);
  await p.evaluate(() => { window.abOpens = []; });
  await p.keyboard.press('k');
  const {s, met} = await until(p, x => x.ok && /^(keyRc\d+|noDisplay)$/.test(x.last));
  if (!met) return check(false, `key not handled: ${seen(s)}`);
  lastLinkAt = Date.now();
  const r = await barState(ctx, p);
  const ok = s.last === 'keyRc0' && !r.opens.length && r.pages === pages && r.href === 'https://example.com/ab-key';
  return check(ok, `${s.last}: ${r.opens.length || r.pages !== pages ? 'tab opened' : 'no tab'}, bar ${r.href.slice(8, 30) || 'none'}`);
}

// #220 recheck: a process under grok asks for a link every 0.55 s (the
// stand-in on "s": spam1..spam6) and the user clicks blank terminal space in
// the middle of it.  No tab may open; the bar shows the latest link.
async function spamCheck(ctx, p) {
  const pages = ctx.pages().length;
  await pastPause();
  await focusTerm(p);
  await p.evaluate(() => { window.abOpens = []; });
  await p.keyboard.press('s');
  // The first spam link, in the bar or (on a page that opens tabs) in a tab.
  let r = await untilBar(ctx, p, x => /\/spam\d$/.test(x.href) || x.opens.length > 0);
  if (!/\/spam\d$/.test(r.href) && !r.opens.length) return check(false, `no spam link: bar ${r.href.slice(8, 30) || 'none'}`);
  const g = await screen(p);
  await p.mouse.click(g.x + g.w / 2, g.y + g.h / 2);
  r = await untilBar(ctx, p, x => /\/spam6$/.test(x.href));
  const ok = /\/spam6$/.test(r.href) && !r.opens.length && r.pages === pages && r.links === 1;
  return check(ok, `${r.opens.length || r.pages !== pages ? `tab opened ${String(r.opens[0] || '').slice(8, 30)}` : 'no tab'}, bar ${r.href.slice(8, 30) || 'none'}`);
}

async function openPage(ctx) {
  await ctx.addInitScript(countWheelReports);
  await ctx.addInitScript(recordOpens);
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
// Returns the page terminal's rows right after the events.  A page that
// follows the visual viewport refits inside the event handler, so there is
// nothing to wait for here; the wait is for grok to redraw at that size.
async function setKeyboard(p, kb, pan) {
  return p.evaluate(([kb, pan]) => {
    const h = innerHeight - kb;
    window.KB = kb ? {height: h, offsetTop: pan ? kb : 0} : null;
    visualViewport.dispatchEvent(new Event('resize'));
    visualViewport.dispatchEvent(new Event('scroll'));
    return term.rows;
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
// the full-height rows again, grok redrawn at them.
async function keyboardCheck(b, device, kb, pan) {
  const ctx = await b.newContext({...devices[device]});
  try {
    await ctx.addInitScript(fakeKeyboard);
    let {p, s, met} = await openPage(ctx);
    if (!met) return check(false, `no grok view at page size: ${seen(s)}`);
    const r0 = s.rows;
    const box = await screen(p);
    await p.touchscreen.tap(box.x + box.w / 2, box.y + box.h / 2);
    const r1 = await setKeyboard(p, kb, pan);
    ({s, met} = await until(p, x => fitted(x) && x.rows === r1));
    if (!met) return check(false, `open ${r0}->${r1}: ${seen(s)}`);
    const v = await rowsVisible(p);
    const r2 = await setKeyboard(p, 0, false);
    ({s, met} = await until(p, x => fitted(x) && x.rows === r2));
    if (!met) return check(false, `closed ${r1}->${r2}: ${seen(s)}`);
    return check(r1 < r0 && v.ok && r2 === r0, `${r0}->${r1}->${r2} ${v.text}`);
  } finally {
    await ctx.close();
  }
}

// Quit, open the "Resume this session?" box from the ended terminal, then
// press the bar's Resume instead: the box must close once grok is live (#57).
async function resumeBoxCheck(b) {
  const ctx = await b.newContext({viewport: {width: 1280, height: 800}});
  try {
    const {p, s, met} = await openPage(ctx);
    if (!met) return check(false, `no grok view at page size: ${seen(s)}`);
    const page = () => p.evaluate(() => ({bar: document.getElementById('stop').textContent,
      status: document.getElementById('status').textContent,
      box: !document.getElementById('resume-ask').hidden}));
    const pageUntil = async pred => {
      const end = Date.now() + WAIT;
      let r = await page();
      while (!pred(r) && Date.now() < end) { await wait(50); r = await page(); }
      return {r, met: pred(r)};
    };
    await p.click('#stop');
    let q = await pageUntil(r => r.bar === 'Resume' && r.status === 'Ended');
    if (!q.met) return check(false, `Quit did not end it: ${q.r.status}`);
    await p.click('#term', {position: {x: 20, y: 20}});
    q = await pageUntil(r => r.box);
    if (!q.met) return check(false, 'ended terminal click opened no box');
    await p.click('#stop');
    q = await pageUntil(r => r.bar === 'Quit' && r.status === 'Running');
    if (!q.met) return check(false, `bar Resume not live: ${q.r.status}`);
    return check(!q.r.box, q.r.box ? 'box still open over live grok' : 'box closed, grok live');
  } finally {
    await ctx.close();
  }
}

async function run() {
  const exe = process.env.AB_CHROME || undefined;
  let b;
  try {
    b = await chromium.launch(exe ? {executablePath: exe} : {});
  } catch (e) {
    return {unavailable: String(e && e.message || e).split('\n')[0]};
  }
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

    // grok's scroll bar is the last column: drag it from the bottom row of
    // the view to the top row.  The stand-in sets last=release when it has
    // handled the drag's release, and every motion before it, so that
    // reading is the drag's end state.  On a page that sends grok no drag
    // the wait runs out.
    const cw = g.w / s.cols, ch = g.h / s.rows, bx = g.x + g.w - cw / 2;
    const low = g.y + g.h - ch * 1.5, high = g.y + ch / 2;
    async function barDrag(ya, yb) {
      await p.mouse.move(bx, ya);
      await p.mouse.down();
      await p.mouse.move(bx, yb, {steps: 10});
      await p.mouse.up();
    }
    const fromOff = s.off;
    await barDrag(low, high);
    ({s, met} = await until(p, x => fitted(x) && x.last === 'release' && x.off <= x.max / 20));
    checks.bar_drag = check(met, `bar to top: off ${fromOff}->${s.off} of ${s.max} last=${s.last}`);
    // Back to the bottom, so the later checks start where grok started.
    if (met) {
      await barDrag(high, low);
      ({s, met} = await until(p, x => fitted(x) && x.last === 'release' && x.off === x.max));
      if (!met) checks.bar_drag = check(false, `bar back to bottom: off ${s.off} of ${s.max} last=${s.last}`);
    }

    const r0 = s.rows, c0 = s.cols;
    await p.setViewportSize({width: 900, height: 600});
    ({s, met} = await until(p, x => fitted(x) && (x.rows !== r0 || x.cols !== c0)));
    checks.resize = check(met, met ? `${r0}x${c0}->${s.termRows}x${s.termCols} grok ${s.rows}x${s.cols}`
                                   : `${r0}x${c0}-> ${seen(s)}`);
    checks.link_open = await linkCheck(ctx, p);
    checks.link_key = await keyCheck(ctx, p);
    checks.link_spam = await spamCheck(ctx, p);
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
    checks.keyboard_android = await keyboardCheck(b, 'Pixel 7', 320, false);
    checks.keyboard_ios = await keyboardCheck(b, 'iPhone 14', 300, true);
    // Last: it quits and resumes the session the other checks share.
    checks.resume_box = await resumeBoxCheck(b);
    return {browser: b.version(), checks};
  } finally {
    await b.close();
  }
}

run().then(r => console.log(JSON.stringify(r)), e => console.log(JSON.stringify({error: String(e && e.message || e).split('\n')[0]})));
