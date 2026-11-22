// Page checks kept as modules in browser.d/ (#243), so a new check is a new
// file.  run.sh starts browser.js with `node -r <file>` for each
// browser.d/<name>.js; each module registers itself:
//   require('../browser_d.js').add(__filename, async t => t.check(ok, value));
// The check's name is the file name without .js (ab.py requires every such
// name).  t holds browser.js's browser and helpers (b, port, sid, url, WAIT,
// wait, check, status, until, fitted, caughtUp, seen, openPage, screen,
// devices); a check opens its own context and closes it.  browser.js runs the
// checks sorted by name, before resume_box.  A check that throws or returns
// no check result fails with the reason; a module that does not load stops
// node, so the browser checks break on that side.
const path = require('path');
const why = e => String(e && e.message || e).split('\n')[0];
const added = [];

function add(file, check) {
  added.push({name: path.basename(String(file), '.js'), check});
}

function list() {
  return added.slice().sort((x, y) => (x.name < y.name ? -1 : x.name > y.name ? 1 : 0));
}

// browser.js merges these results into its own checks by name, so a module
// that reuses a built-in name would replace that check's result (#243 r2-1).
function clashes(mods, taken) {
  return mods.map(m => m.name).filter(n => taken.includes(n));
}

async function runAll(mods, t) {
  const out = {};
  for (const m of mods) {
    try {
      if (typeof m.check !== 'function') throw new Error('add() needs a check function');
      const r = await m.check(t);
      out[m.name] = r && typeof r.ok === 'boolean' ? r : t.check(false, `no check result: ${JSON.stringify(r)}`);
    } catch (e) {
      out[m.name] = t.check(false, `threw: ${why(e)}`);
    }
  }
  return out;
}

module.exports = {add, list, clashes, runAll};
