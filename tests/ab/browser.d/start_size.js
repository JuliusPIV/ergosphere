// start_size (#58): grok starts at the page terminal's size, not 80x24.
// Create on the list page, then Quit and Resume on the shared session's page.
// The page sends /launch or /resume and attaches /term only after the reply,
// and only /term resizes the PTY, so the PTY size read while the reply is held
// is the size grok started at.  Each must equal the page terminal's size.
const {execFileSync} = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

// Pids of processes with id as a whole argument.
function pidsOf(id) {
  return fs.readdirSync('/proc').filter(n => /^\d+$/.test(n)).filter(pid => {
    try { return fs.readFileSync(`/proc/${pid}/cmdline`, 'utf8').split('\0').includes(id); } catch (e) { return false; }
  });
}
// The PTY size ("<rows>x<cols>") of a process with id as a whole argument
// that is not in old, or '' when there is none yet.  stdin of the stand-in
// (and its wrapper) is the PTY.
function ptySize(id, old) {
  for (const pid of pidsOf(id).filter(x => !old.includes(x))) {
    try {
      const out = execFileSync('stty', ['-F', `/proc/${pid}/fd/0`, 'size'], {encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore']});
      const [rows, cols] = out.trim().split(/\s+/);
      if (rows && cols) return `${rows}x${cols}`;
    } catch (e) { /* gone, or not ours */ }
  }
  return '';
}

// Holds the reply to POST <route> until the started grok's PTY size is read;
// .got then resolves to it ('-' when no such process showed up).
async function holdReply(t, p, route, idOf, old = []) {
  let done;
  const got = new Promise(r => { done = r; });
  await p.route(`**${route}`, async r => {
    let size = '-';
    try {
      const resp = await r.fetch();
      const id = idOf(await resp.text());
      const end = Date.now() + t.WAIT;
      while (id && !(size = ptySize(id, old)) && Date.now() < end) await t.wait(50);
      size = size || '-';
      await r.fulfill({response: resp});
    } catch (e) {
      try { await r.continue(); } catch (e2) { /* page gone */ }
    }
    done(size);
  }, {times: 1});
  return {got};
}

require('../browser_d.js').add(__filename, async t => {
  const ctx = await t.b.newContext({viewport: {width: 1280, height: 800}});
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), 'ab-start-size-'));
  try {
    const p = await ctx.newPage();
    p.setDefaultTimeout(t.WAIT);
    await p.goto(`http://127.0.0.1:${t.port}/`);
    const made = await holdReply(t, p, '/launch', body => {
      try { return JSON.parse(body).id; } catch (e) { return ''; }
    });
    await p.click('a[data-kind="new"]');
    await p.fill('#na-cwd', cwd);
    await p.click('#na-create');
    const start1 = await made.got;
    await p.waitForURL(/\/chat\?resume=/);
    let {s, met} = await t.until(p, t.fitted);
    if (!met) return t.check(false, `new: no grok view at page size: ${t.seen(s)}`);
    const page1 = `${s.termRows}x${s.termCols}`;
    // Quit, until the bar offers Resume.  The new session is quit too: later
    // checks (settings_poll) count the live sessions.
    const quit = async () => {
      const bar = () => p.evaluate(() => document.getElementById('stop').textContent + ' ' +
        document.getElementById('status').textContent);
      await p.click('#stop');
      const end = Date.now() + t.WAIT;
      while (await bar() !== 'Resume Ended' && Date.now() < end) await t.wait(50);
      return bar();
    };
    let ended = await quit();
    if (ended !== 'Resume Ended') return t.check(false, `new: Quit did not end it: ${ended}`);
    await p.goto(t.url);
    ({s, met} = await t.until(p, t.fitted));
    if (!met) return t.check(false, `no grok view at page size: ${t.seen(s)}`);
    ended = await quit();
    if (ended !== 'Resume Ended') return t.check(false, `Quit did not end it: ${ended}`);
    const back = await holdReply(t, p, '/resume', () => t.sid, pidsOf(t.sid));
    await p.click('#stop');
    const start2 = await back.got;
    ({s, met} = await t.until(p, t.fitted));
    if (!met) return t.check(false, `resume: no grok view at page size: ${t.seen(s)}`);
    const page2 = `${s.termRows}x${s.termCols}`;
    return t.check(start1 === page1 && start2 === page2, `new ${start1} page ${page1}; resume ${start2} page ${page2}`);
  } finally {
    await ctx.close();
    fs.rmSync(cwd, {recursive: true, force: true});
  }
});
