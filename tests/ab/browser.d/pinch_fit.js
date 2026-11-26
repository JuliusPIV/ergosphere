// pinch_fit (#117): pinch the font size from 8 to 28 px and back, one size
// per touch move.  After each move the width left over is under one cell and
// not below zero (xterm rounds the cell width over the old columns, so one
// fit could land a column short).  Then grok draws at the final size.
require('../browser_d.js').add(__filename, async t => {
  const ctx = await t.b.newContext({...t.devices['Pixel 7']});
  try {
    let {p, s, met} = await t.openPage(ctx);
    if (!met) return t.check(false, `no grok view at page size: ${t.seen(s)}`);
    const cdp = await ctx.newCDPSession(p);
    const g = await t.screen(p);
    const cx = g.x + g.w / 2, cy = g.y + g.h / 2, D0 = 100;
    const pts = d => [{x: cx - d / 2, y: cy, id: 1}, {x: cx + d / 2, y: cy, id: 2}];
    const at = n => p.evaluate(n => {
      if (term.options.fontSize !== n) return null;
      const w = parseInt(getComputedStyle(term.element.parentElement).width);
      const cell = term._core._renderService.dimensions.css.cell.width;
      // Rounded: 412 - 76 * (412 / 76) is -6e-14, not below zero.
      return {cols: term.cols, gap: Math.round((w - term.cols * cell) * 100) / 100, cell};
    }, n);
    const bad = [];
    async function pinch(sizes) {
      const size0 = await p.evaluate(() => term.options.fontSize);
      await cdp.send('Input.dispatchTouchEvent', {type: 'touchStart', touchPoints: pts(D0)});
      for (const n of sizes) {
        await cdp.send('Input.dispatchTouchEvent', {type: 'touchMove', touchPoints: pts(D0 * n / size0)});
        const end = Date.now() + t.WAIT;
        let r = await at(n);
        while (!r && Date.now() < end) { await t.wait(50); r = await at(n); }
        if (!r) bad.push(`${n}px:font`);
        else if (!(r.gap >= 0 && r.gap < r.cell)) bad.push(`${n}px:${r.cols}c`);
      }
      await cdp.send('Input.dispatchTouchEvent', {type: 'touchEnd', touchPoints: []});
    }
    const up = [], down = [];
    for (let n = 9; n <= 28; n++) up.push(n);
    for (let n = 27; n >= 8; n--) down.push(n);
    await pinch([8]);
    bad.length = 0;
    await pinch(up);
    await pinch(down);
    ({s, met} = await t.until(p, t.fitted));
    return t.check(met && !bad.length, `${bad.length} of 40 short${bad.length ? ': ' + bad.join(' ') : ''}, grok ${s.rows}x${s.cols}`);
  } finally {
    await ctx.close();
  }
});
