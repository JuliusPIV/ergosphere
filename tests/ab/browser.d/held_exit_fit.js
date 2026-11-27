// held_exit_fit (#186): a page held by a grok this server did not start, then
// that grok exits: the page shows Ended and its terminal must fit the page
// then.  The holder is a stand-in whose argv[0] is "grok" and whose command
// line names a fresh id, so the check leaves the shared session alone.
const {spawn} = require('child_process');
const {randomUUID} = require('crypto');

require('../browser_d.js').add(__filename, async t => {
  const hsid = randomUUID();
  const base = `http://127.0.0.1:${t.port}`;
  const held = async () => (await (await fetch(`${base}/holder?id=${hsid}`, {cache: 'no-store'})).json()).held;
  const holdUntil = async want => {
    const end = Date.now() + t.WAIT;
    while (Date.now() < end) { if (await held() === want) return true; await t.wait(50); }
    return false;
  };
  const holder = spawn('python3',
    ['-c', 'import time; time.sleep(600)', '-r', hsid], {argv0: 'grok', stdio: 'ignore'});
  const ctx = await t.b.newContext({viewport: {width: 1280, height: 800}});
  try {
    if (!await holdUntil(true)) return t.check(false, 'stand-in holder not seen');
    const p = await ctx.newPage();
    p.setDefaultTimeout(t.WAIT);
    await p.goto(`${base}/chat?resume=${hsid}`);
    const page = () => p.evaluate(() => {
      let want = null;
      try { want = fit.proposeDimensions() || null; } catch (err) {}
      return {hold, ended, status: document.getElementById('status').textContent,
        cols: term.cols, rows: term.rows, want: want ? {cols: want.cols, rows: want.rows} : null};
    });
    const pageUntil = async pred => {
      const end = Date.now() + t.WAIT;
      let r = await page();
      while (!pred(r) && Date.now() < end) { await t.wait(25); r = await page(); }
      return {r, met: pred(r)};
    };
    let q = await pageUntil(r => r.hold);
    if (!q.met) return t.check(false, `page not held: ${q.r.status}`);
    holder.kill();
    if (!await holdUntil(false)) return t.check(false, 'holder exit not seen');
    // The fit, when the page makes one, runs in the step that shows Ended.
    q = await pageUntil(r => r.ended);
    if (!q.met) return t.check(false, `not Ended: ${q.r.status}`);
    const r = q.r, w = r.want || {};
    return t.check(r.status === 'Ended' && r.cols === w.cols && r.rows === w.rows,
      `Ended at ${r.cols}x${r.rows}, page fits ${w.cols}x${w.rows}`);
  } finally {
    holder.kill();
    await ctx.close();
  }
});
