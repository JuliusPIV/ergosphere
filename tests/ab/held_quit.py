"""A/B check for issue #134: a page whose session another server runs offers no Quit.

  python3 -B held_quit.py run <code-dir> <port> <holder-port> <work-dir>  -> JSON checks on stdout
  python3 -B held_quit.py summary <A-json> <B-json>                       -> table on stdout;
                                                                           exit 1 when a check fails on B

Two servers of <code-dir> on one throwaway HOME: the page server on <port>,
the holder server on <holder-port>.  The holder server runs the session's
grok (the stand-in TUI, fakegrok.py), so the page server's chat page is held
by "another Ergosphere server".  Headless Chrome (playwright) drives the page
server through every way a page gets into or out of that held state.  When a
step finds Quit offered on a held page it presses it, as a user would, so the
"quitting" that never ends shows up as a failed check.
Stdlib plus node and playwright.  No real grok, no model, no network.
"""
import json
import os
import shutil
import subprocess
import sys
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
ORDER = [
    "load_no_quit", "load_ends",
    "resume_no_quit", "resume_ends",
    "pending_not_stuck", "pending_ends",
    "take_quit_back", "take_quit_ends",
    "refused_no_quit", "refused_ends",
    "tabs_no_quit", "tabs_end",
    "server_gone_no_quit",
]

BROWSER_JS = r"""
const {chromium} = require('playwright');
const [pageBase, holdBase, serverPid, chrome, sidsJson] = process.argv.slice(1);
const sids = JSON.parse(sidsJson);
const WAIT = 30000;
// The page polls /holder once a second: Ended this long after the holder exits is the bug.
const END_WAIT = 15000;
const checks = {};
const ck = (name, ok, value) => { checks[name] = {ok: !!ok, value: String(value).slice(0, 60)}; };
const sleep = ms => new Promise(r => setTimeout(r, ms));
async function holdFetch(path, body) {
  const opt = body === undefined ? {} : {method: 'POST', body: JSON.stringify(body),
    headers: {'Content-Type': 'application/json', 'Origin': holdBase}};
  const resp = await fetch(holdBase + path, opt);
  return resp.json();
}
async function waitFor(fn, wait) {
  const end = Date.now() + (wait || WAIT);
  let v = await fn();
  while (!v && Date.now() < end) { await sleep(50); v = await fn(); }
  return v;
}
// The holder server runs the session's grok: a stand-in in the holder server's PTY.
async function holdStart(sid) {
  await holdFetch('/resume', {id: sid});
  const info = await waitFor(async () => {
    const d = await holdFetch('/session-info?id=' + sid);
    return d.here && d.pid ? d : null;
  });
  if (!info) throw new Error('holder server did not start ' + sid);
  return info.pid;
}
async function holdEnd(pid) {
  try { process.kill(pid, 'SIGTERM'); } catch (e) {}
}
const look = p => p.evaluate(() => {
  const stop = document.getElementById('stop');
  return {bar: stop.textContent, quit: !stop.hidden && stop.offsetParent !== null,
          status: document.getElementById('status').textContent};
});
const show = s => (s.quit ? s.bar + ' shown' : 'no button') + ', ' + s.status;
async function until(p, pred, wait) {
  const end = Date.now() + (wait || WAIT);
  let s = await look(p);
  while (!pred(s) && Date.now() < end) { await sleep(50); s = await look(p); }
  return s;
}
const isHeld = s => s.status === 'session is already running';
const isEnded = s => s.status === 'Ended' && s.bar === 'Resume' && s.quit;
// A user who sees Quit on a held page presses it.
async function pressIfShown(p) {
  const s = await look(p);
  if (s.quit && s.bar === 'Quit') await p.click('#stop');
  return s;
}
const chatUrl = sid => pageBase + '/chat?resume=' + encodeURIComponent(sid);
async function endsAfterHolder(p, pid, name) {
  await holdEnd(pid);
  const s = await until(p, isEnded, END_WAIT);
  ck(name, isEnded(s), show(s));
}

(async () => {
  const b = await chromium.launch({executablePath: chrome});
  const ctx = await b.newContext({viewport: {width: 1280, height: 800}});
  const page = async () => { const p = await ctx.newPage(); p.setDefaultTimeout(WAIT); return p; };
  try {
    // 1. Opened while the other server runs it (the report).
    let pid = await holdStart(sids.load);
    let p = await page();
    await p.goto(chatUrl(sids.load));
    let s = await until(p, isHeld);
    ck('load_no_quit', isHeld(s) && !s.quit, show(s));
    await pressIfShown(p);
    await endsAfterHolder(p, pid, 'load_ends');
    await p.close();

    // 2. Opened stopped, the other server starts it, then Resume here.
    p = await page();
    await p.goto(chatUrl(sids.resume));
    await until(p, s => s.bar === 'Resume');
    pid = await holdStart(sids.resume);
    await p.click('#stop');
    s = await until(p, isHeld);
    ck('resume_no_quit', isHeld(s) && !s.quit, show(s));
    await pressIfShown(p);
    await endsAfterHolder(p, pid, 'resume_ends');
    await p.close();

    // 3. Quit pressed while that Resume's reply is still on its way.
    p = await page();
    await p.goto(chatUrl(sids.pending));
    await until(p, s => s.bar === 'Resume');
    pid = await holdStart(sids.pending);
    let release = null;
    const caught = new Promise(r => {
      p.route('**/resume', route => { release = () => route.continue(); r(); });
    });
    await p.click('#stop');
    await caught;
    await p.click('#stop');
    release();
    s = await until(p, s => s.status !== 'quitting' && s.status !== 'starting', END_WAIT);
    ck('pending_not_stuck', s.status !== 'quitting' && !(isHeld(s) && s.quit), show(s));
    await endsAfterHolder(p, pid, 'pending_ends');
    await p.close();

    // 4. Take over: Quit comes back for this server's own grok.
    pid = await holdStart(sids.take);
    p = await page();
    p.on('dialog', d => d.accept());
    await p.goto(chatUrl(sids.take));
    await until(p, isHeld);
    await p.click('#take');
    s = await until(p, s => s.status === 'Running' && s.quit);
    ck('take_quit_back', s.status === 'Running' && s.quit && s.bar === 'Quit', show(s));
    await p.click('#stop');
    s = await until(p, isEnded, END_WAIT);
    ck('take_quit_ends', isEnded(s), show(s));
    await p.close();

    // 5. Take over refused: still held, still no Quit.
    pid = await holdStart(sids.refused);
    p = await page();
    p.on('dialog', d => d.accept());
    await p.route('**/take*', route => route.fulfill({status: 409, contentType: 'application/json',
      body: JSON.stringify({ok: false, error: 'refused by the test'})}));
    await p.goto(chatUrl(sids.refused));
    await until(p, isHeld);
    await p.click('#take');
    s = await until(p, s => s.status === 'refused by the test');
    ck('refused_no_quit', s.status === 'refused by the test' && !s.quit, show(s));
    await pressIfShown(p);
    await endsAfterHolder(p, pid, 'refused_ends');
    await p.close();

    // 6. Two tabs on the held session: neither offers Quit, both end.
    pid = await holdStart(sids.tabs);
    const t1 = await page(), t2 = await page();
    await t1.goto(chatUrl(sids.tabs));
    await t2.goto(chatUrl(sids.tabs));
    const s1 = await until(t1, isHeld), s2 = await until(t2, isHeld);
    ck('tabs_no_quit', isHeld(s1) && isHeld(s2) && !s1.quit && !s2.quit, show(s1) + ' | ' + show(s2));
    await pressIfShown(t1);
    await holdEnd(pid);
    const e1 = await until(t1, isEnded, END_WAIT), e2 = await until(t2, isEnded, END_WAIT);
    ck('tabs_end', isEnded(e1) && isEnded(e2), show(e1) + ' | ' + show(e2));
    await t1.close(); await t2.close();

    // 7. The page's own server stops while it is held (last: the server is gone after).
    pid = await holdStart(sids.gone);
    p = await page();
    await p.goto(chatUrl(sids.gone));
    await until(p, isHeld);
    const failed = new Promise(r => p.on('requestfailed', q => { if (q.url().includes('/holder')) r(); }));
    process.kill(Number(serverPid), 'SIGTERM');
    await Promise.race([failed, sleep(WAIT)]);
    s = await look(p);
    ck('server_gone_no_quit', isHeld(s) && !s.quit, show(s));
    await holdEnd(pid);
  } finally {
    await b.close();
  }
  console.log(JSON.stringify({checks}));
})().catch(e => { console.log(JSON.stringify({checks, error: String(e && e.message || e)})); process.exit(1); });
"""


def check(ok, value):
    return {"ok": bool(ok), "value": str(value)[:60]}


def wait_for(fn, deadline=60):
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        if fn():
            return True
        time.sleep(0.1)
    return False


def seed(home, sid, cwd):
    folder = os.path.join(home, ".grok", "sessions", "ab", sid)
    os.makedirs(folder)
    os.makedirs(cwd, exist_ok=True)
    with open(os.path.join(folder, "summary.json"), "w") as handle:
        json.dump({"info": {"id": sid, "cwd": cwd}, "generated_title": "held quit check",
                   "updated_at": "2026-01-01T00:00:00Z"}, handle)


def browser():
    """(node, chrome, NODE_PATH) or a reason the browser check cannot run."""
    node = shutil.which("node")
    chrome = os.environ.get("ERGO_CHROME") or os.environ.get("AB_CHROME") or "/usr/bin/google-chrome"
    if not node or not os.path.exists(chrome):
        return "no node or Chrome"
    if subprocess.run([node, "-e", "require('playwright')"], capture_output=True, timeout=20).returncode:
        return "no playwright module for node (set NODE_PATH)"
    return node, chrome


def start_server(code, port, home, bin_dir, work, name):
    config = os.path.join(work, name + ".json")
    with open(config, "w") as handle:
        json.dump({"bind": "127.0.0.1", "port": int(port)}, handle)
    env = dict(os.environ, HOME=home, ERGOSPHERE_CONFIG=config, PATH=bin_dir + ":" + os.environ["PATH"])
    env.pop("ERGOSPHERE_TOKEN", None)
    env.pop("ERGOSPHERE_PORT", None)
    logpath = os.path.join(work, name + ".log")
    log = open(logpath, "w")
    proc = subprocess.Popen([sys.executable, "-B", os.path.join(code, "ergosphere.py"),
                             "--bind", "127.0.0.1", "--port", str(port)],
                            cwd=work, env=env, stdout=log, stderr=subprocess.STDOUT)
    log.close()
    # The ready line, not a time: a loaded machine can take long to start.
    wait_for(lambda: "ERGOSPHERE_READY" in open(logpath).read() or proc.poll() is not None)
    return proc, "ERGOSPHERE_READY" in open(logpath).read()


def stop(proc):
    if proc.poll() is None:
        proc.terminate()
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def run(code, port, hold_port, work):
    found = browser()
    if isinstance(found, str):
        return {"unavailable": found}
    node, chrome = found
    home, bin_dir = os.path.join(work, "home"), os.path.join(work, "bin")
    os.makedirs(bin_dir)
    # Fresh ids: the holder scan is machine-wide, so concurrent runs never meet.
    sids = {name: str(uuid.uuid4()) for name in ("load", "resume", "pending", "take", "refused", "tabs", "gone")}
    for name, sid in sids.items():
        seed(home, sid, os.path.join(work, "work-" + name))
    with open(os.path.join(bin_dir, "grok"), "w") as handle:
        # argv[0] must be "grok": Ergosphere checks /proc/<pid>/cmdline.
        handle.write('#!/bin/bash\nexec -a grok python3 -B "%s/fakegrok.py" "$@"\n' % HERE)
    os.chmod(os.path.join(bin_dir, "grok"), 0o755)
    holder, hold_ok = start_server(code, hold_port, home, bin_dir, work, "holder")
    pager, page_ok = start_server(code, port, home, bin_dir, work, "page")
    try:
        if not (hold_ok and page_ok):
            return {"checks": {n: check(False, "server not ready") for n in ORDER}}
        out = subprocess.run(
            [node, "-e", BROWSER_JS, "http://127.0.0.1:%s" % port, "http://127.0.0.1:%s" % hold_port,
             str(pager.pid), chrome, json.dumps(sids)],
            capture_output=True, text=True, timeout=400, env=dict(os.environ, HOME=home))
        try:
            res = json.loads(out.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            res = {"checks": {}, "error": "node: %s" % out.stderr.strip()[-300:]}
        checks = res.get("checks") or {}
        for name in ORDER:
            checks.setdefault(name, check(False, "not reached: %s" % res.get("error", "")))
        res["checks"] = checks
        return res
    finally:
        stop(pager)
        stop(holder)


def summary(apath, bpath):
    def load(path):
        try:
            with open(path) as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return {"error": "no result"}
    a, b = load(apath), load(bpath)
    width = 36
    lines = ["", "Issue #134: Quit on a page whose session another Ergosphere server runs"]
    if "unavailable" in b or "unavailable" in a:
        lines.append("BROWSER CHECKS DID NOT RUN: %s" % (b.get("unavailable") or a.get("unavailable")))
        print("\n".join(lines))
        return 1
    lines.append("%-20s %-*s %-*s %s" % ("check", width, "A", width, "B", "same"))
    failed = []
    for name in ORDER:
        ca = (a.get("checks") or {}).get(name) or {"ok": False, "value": "not run"}
        cb = (b.get("checks") or {}).get(name) or {"ok": False, "value": "not run"}
        cell = lambda c: ("%s %s" % ("PASS" if c["ok"] else "FAIL", c["value"]))[:width]
        if not cb["ok"]:
            failed.append(name)
        lines.append("%-20s %-*s %-*s %s" % (name, width, cell(ca), width, cell(cb), "yes" if ca == cb else "NO"))
    if b.get("error"):
        lines.append("B browser error: %s" % b["error"][:300])
    if failed:
        lines.append("FAILS ON B: %s" % ", ".join(failed))
    print("\n".join(lines))
    return 1 if failed else 0


def main(argv):
    if len(argv) == 6 and argv[1] == "run":
        print(json.dumps(run(argv[2], argv[3], argv[4], argv[5])))
        return 0
    if len(argv) == 4 and argv[1] == "summary":
        return summary(argv[2], argv[3])
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
