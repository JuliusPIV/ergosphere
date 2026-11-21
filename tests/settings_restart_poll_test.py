"""Settings follows the server's running count while the page is open (#49).

The Settings page script runs in node against a stub DOM.  Its fetch answers
/session-info with list_live() rows built from this server's LIVE table, as
the real handler does.  After each poll the shown count must equal
running_sessions() (the restart gate's count) and the Restart button must be
enabled only at zero.  While a restart is in progress a poll must not
re-enable the button.

Run: python3 -B tests/settings_restart_poll_test.py
Stdlib plus node, no network, no grok, no server.  Fails without node.
"""
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DRIVER = r"""
const fs = require('fs');
const vm = require('vm');
const [scriptPath, stepsPath] = process.argv.slice(2);
const steps = JSON.parse(fs.readFileSync(stepsPath, 'utf8'));
const nodes = {};
function node(id) {
  return {
    id, textContent: '', disabled: false, value: '', className: '', children: [], listeners: {},
    addEventListener(t, f) { this.listeners[t] = f; },
    appendChild(c) { this.children.push(c); },
    setAttribute() {}, getAttribute() { return null; },
    querySelectorAll() { return []; },
  };
}
const document = {
  hidden: false,
  getElementById(id) { return nodes[id] || (nodes[id] = node(id)); },
  createElement() { return node(''); },
  createTextNode() { return node(''); },
};
const intervals = [];
let infoBody = null;
let refusal = null;
let healthDown = false;
let hold = false;
let held = null;
function reply(body, status) {
  status = status || 200;
  return Promise.resolve({ok: status === 200, status, json: () => Promise.resolve(body)});
}
function fetch(url) {
  if (url === '/session-info' && hold) return new Promise((r) => { held = r; });
  if (url === '/session-info') return reply(infoBody);
  // Restart: /health fails with step.healthDown, is refused with step.refuse,
  // else /health never answers.
  if (healthDown && url === '/health') return Promise.reject(new Error('down'));
  if (refusal && url === '/health') return reply({started: 1});
  if (refusal && url === '/settings') return reply(refusal, 409);
  return new Promise(() => {});
}
const ctx = {document, fetch, location: {origin: 'http://127.0.0.1:1', hostname: '127.0.0.1', protocol: 'http:'},
  setInterval: (f) => { intervals.push(f); return intervals.length; }, setTimeout: () => 0,
  JSON, Object, Array, String, parseInt, isNaN};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(scriptPath, 'utf8'), ctx);
const settle = () => new Promise((r) => setImmediate(() => setImmediate(() => setImmediate(r))));
(async () => {
  const out = [];
  for (const step of steps) {
    refusal = step.refuse || null;
    healthDown = !!step.healthDown;
    infoBody = step.body;
    hold = !!step.hold;
    if (step.click) { nodes.restart.listeners.click(); await settle(); }
    if (step.release) {
      held({ok: true, status: 200, json: () => Promise.resolve(step.release)});
      held = null;
    } else {
      for (const f of intervals) f();
    }
    await settle();
    out.push({text: nodes.sessions.textContent, disabled: nodes.restart.disabled, msg: document.getElementById('restart-msg').textContent});
  }
  console.log(JSON.stringify(out));
})();
"""


def load():
    spec = importlib.util.spec_from_file_location("ergosphere", ROOT / "ergosphere.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeProc:
    def __init__(self, pid, code=None):
        self.pid = pid
        self.code = code

    def poll(self):
        return self.code


def page_script(page):
    start = page.index("<script>\n") + len("<script>\n")
    return page[start:page.index("</script>", start)]


def main():
    if not shutil.which("node"):
        print("FAIL: node not found")
        return 1
    erg = load()
    state = {"bind": "", "port": "", "runningBind": "127.0.0.1", "runningPort": 1, "binds": ["127.0.0.1"],
             "agents": {}, "names": [], "overrides": []}

    def live(sid, code=None, closed=False):
        entry = erg.Live(sid, FakeProc(1000 + len(erg.LIVE), code), -1, "/")
        entry.buffer.extend(b"x")
        entry.closed = closed
        return entry

    # Each step: the LIVE table as the server holds it, then one poll.
    tables = [
        ("one running", {"a": live("a")}),
        ("it ended in another tab", {}),
        ("a new one started", {"b": live("b")}),
        ("one stopping, one exited", {"c": live("c", closed=True), "d": live("d", code=0)}),
        ("all ended", {}),
    ]
    steps, want = [], []
    for name, table in tables:
        erg.LIVE.clear()
        erg.LIVE.update(table)
        steps.append({"body": erg.live_info()})
        want.append((name, erg.running_sessions()))
    # A spawn blocked in STARTING is a running session for the restart gate
    # but has no LIVE row: the poll must still count it (and keep Restart off).
    erg.STARTING.add("spawning")
    steps.append({"body": erg.live_info()})
    want.append(("blocked spawn, no live session", erg.running_sessions()))
    erg.STARTING.clear()
    # Restart refused (a session started first), then that session ends:
    # the refusal must not stay on screen next to an enabled button.
    busy = "1 grok session running; restart needs zero"
    one = {"sessions": [{"id": "e", "alive": True}], "running": 1}
    steps.append({"click": True, "refuse": {"ok": False, "sessions": 1, "error": busy}, "body": one})
    steps.append({"body": {"sessions": [], "running": 0}})
    # A poll goes out at zero and its reply is held; a session starts and a
    # restart is refused with a count of one; then the old reply lands.  It
    # must not reset the count to zero, clear the refusal or enable Restart.
    steps.append({"hold": True, "body": None})
    steps.append({"click": True, "refuse": {"ok": False, "sessions": 1, "error": busy},
                  "release": {"sessions": [], "running": 0}})
    # Restart pressed at zero; while its request is out a session starts and
    # ends again.  The poll must not re-enable the button.
    steps.append({"click": True, "body": {"sessions": [{"id": "e", "alive": True}], "running": 1}})
    steps.append({"body": {"sessions": [], "running": 0}})
    # #156: with one session running, the restart's /health check fails, or
    # the restart is refused without a count.  Restart must stay disabled,
    # also after the next poll at the same count.
    steps.append({"click": True, "healthDown": True, "body": one})
    steps.append({"click": True, "refuse": {"ok": False, "error": "could not restart"}, "body": one})
    erg.LIVE.clear()

    state["sessions"] = 1
    failed = 0
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp, "settings.js")
        script.write_text(page_script(erg.settings_page(state)))
        steps_file = Path(tmp, "steps.json")
        steps_file.write_text(json.dumps(steps))
        driver = Path(tmp, "driver.js")
        driver.write_text(DRIVER)
        run = subprocess.run(["node", str(driver), str(script), str(steps_file)],
                             capture_output=True, text=True, timeout=60)
        if run.returncode != 0:
            print("FAIL: node exited %d: %s" % (run.returncode, run.stderr.strip()[-400:]))
            return 1
        got = json.loads(run.stdout)
    for (name, count), seen in zip(want, got):
        text = "Running in this server: %d." % count
        ok = seen["text"].startswith(text) and seen["disabled"] == (count != 0)
        print("%s %s: server counts %d; page shows %r, button %s" % (
            "PASS" if ok else "FAIL", name, count, seen["text"], "disabled" if seen["disabled"] else "enabled"))
        failed += not ok
    refused, cleared, stale = got[len(want)], got[len(want) + 1], got[len(want) + 3]
    if refused["disabled"] and refused["msg"] == busy:
        print("PASS refused restart: button disabled, %r shown" % refused["msg"])
    else:
        print("FAIL refused restart: got %r" % refused)
        failed += 1
    if not cleared["disabled"] and cleared["msg"] == "" and cleared["text"].startswith("Running in this server: 0."):
        print("PASS session ended after the refusal: count 0, button enabled, refusal cleared")
    else:
        print("FAIL session ended after the refusal: page shows %r, button %s, message %r" % (
            cleared["text"], "disabled" if cleared["disabled"] else "enabled", cleared["msg"]))
        failed += 1
    if stale["disabled"] and stale["msg"] == busy and stale["text"].startswith("Running in this server: 1."):
        print("PASS poll reply older than a refusal: count 1, button disabled, %r kept" % stale["msg"])
    else:
        print("FAIL poll reply older than a refusal: page shows %r, button %s, message %r" % (
            stale["text"], "disabled" if stale["disabled"] else "enabled", stale["msg"]))
        failed += 1
    for name, seen in (("restart check failed", got[-2]), ("refused without a count", got[-1])):
        if seen["disabled"] and seen["text"].startswith("Running in this server: 1."):
            print("PASS %s with 1 session: button disabled" % name)
        else:
            print("FAIL %s with 1 session: page shows %r, button %s" % (
                name, seen["text"], "disabled" if seen["disabled"] else "enabled"))
            failed += 1
    last = got[-3]
    if last["disabled"]:
        print("PASS restart in progress: polls (1 then 0) leave the button disabled")
    else:
        print("FAIL restart in progress: a poll re-enabled the button")
        failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
