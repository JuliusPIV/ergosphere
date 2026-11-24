// input_latency (#151): every check waits up to 30 s for its condition, so a
// change that answers 5 s late passes them all.  One wheel notch, alternately
// up and down so the view stays put, until the stand-in has handled it;
// SAMPLES times on a fresh page.  The check carries each round trip in ms;
// ab.py compares the two sides' medians and fails B when it is much slower.
const SAMPLES = 7;

require('../browser_d.js').add(__filename, async t => {
  const ctx = await t.b.newContext({viewport: {width: 1280, height: 800}});
  try {
    let {p, s, met} = await t.openPage(ctx);
    if (!met) return t.check(false, `no grok view at page size: ${t.seen(s)}`);
    const g = await t.screen(p);
    await p.mouse.move(g.x + g.w / 2, g.y + g.h / 2);
    const samples = [];
    for (let i = 0; i < SAMPLES; i++) {
      const from = s, t0 = Date.now();
      await p.mouse.wheel(0, i % 2 ? 100 : -100);
      ({s, met} = await t.until(p, t.caughtUp(from)));
      if (!met) return t.check(false, `notch ${i + 1} not handled: ${t.seen(s)}`);
      samples.push(Date.now() - t0);
    }
    return {...t.check(true, `${samples.length} wheel round trips handled`), samples};
  } finally {
    await ctx.close();
  }
});
