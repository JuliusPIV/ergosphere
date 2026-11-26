// row_seams (#69): lines between rows at a fractional device scale.  Paint
// every cell grey in the page's terminal, screenshot it in device pixels and
// count pixel rows where most of the width is not that grey.  The value also
// gives the fitted rows x cols, so A and B show the fit side by side.
require('../browser_d.js').add(__filename, async t => {
  const out = [];
  let ok = true;
  for (const [dsf, font] of [[2.625, 13], [3.5, 13], [3, 13]]) {
    const ctx = await t.b.newContext({viewport: {width: 412, height: 839}, isMobile: true, hasTouch: true, deviceScaleFactor: dsf});
    try {
      await ctx.addInitScript(f => { try { localStorage.setItem('ergosphere.fontSize', String(f)); } catch (e) {} }, font);
      const {p, s, met} = await t.openPage(ctx);
      if (!met) return t.check(false, `${dsf}x: no grok view at page size: ${t.seen(s)}`);
      await p.evaluate(() => new Promise(r => term.write('\x1b[48;2;64;64;64m\x1b[2J',
        () => requestAnimationFrame(() => requestAnimationFrame(r)))));
      const g = await t.screen(p);
      const png = await p.screenshot({clip: {x: g.x, y: g.y, width: g.w, height: g.h}});
      const n = await p.evaluate(async b64 => {
        const img = new Image();
        img.src = 'data:image/png;base64,' + b64;
        await img.decode();
        const c = document.createElement('canvas');
        c.width = img.width; c.height = img.height;
        const x = c.getContext('2d');
        x.drawImage(img, 0, 0);
        const d = x.getImageData(0, 0, c.width, c.height).data;
        let seams = 0;
        for (let y = 0; y < c.height; y++) {
          let off = 0;
          for (let i = y * c.width * 4, e = i + c.width * 4; i < e; i += 4) {
            if (Math.abs(d[i] - 64) > 2 || Math.abs(d[i + 1] - 64) > 2 || Math.abs(d[i + 2] - 64) > 2) off++;
          }
          if (off > c.width / 2) seams++;
        }
        return seams;
      }, png.toString('base64'));
      if (n) ok = false;
      out.push(`${dsf}:${s.termRows}x${s.termCols} ${n}`);
    } finally {
      await ctx.close();
    }
  }
  return t.check(ok, out.join(' '));
});
