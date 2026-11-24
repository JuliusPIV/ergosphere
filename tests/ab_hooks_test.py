"""A new A/B check is a new file (#243): tests/ab/checks/*.sh and tests/ab/browser.d/*.js.

- run_checks.sh runs every checks/*.sh; a passing one adds its rows, and a
  failing, crashing or silent one, or one the base tree has and this tree
  lost, fails.
- browser_d.js loads every browser.d/*.js; a module that fails to load, has
  the wrong shape, throws or returns nothing fails under its file name.
- ab.py summary requires every browser.d/ name, from this tree and the base
  tree (AB_A_DIR), so a module B dropped or never ran fails.
- AGENTS.md tells agents to add checks this way.

Run: python3 -B tests/ab_hooks_test.py   (stdlib; the browser_d part needs node)
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AB = ROOT / "tests" / "ab"
failures = []


def expect(ok, what):
    print("%s %s" % ("PASS" if ok else "FAIL", what))
    if not ok:
        failures.append(what)


def write(path, text, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)


def run_checks(tmp, checks, base=None):
    out = tmp / "out"
    shutil.rmtree(out, ignore_errors=True)
    args = ["bash", str(AB / "run_checks.sh"), str(checks)] + ([str(base)] if base else [])
    r = subprocess.run(args, capture_output=True, text=True, timeout=60,
                       env=dict(os.environ, AB_OUT=str(out)))
    return r.returncode, r.stdout


def test_run_checks(tmp):
    d = tmp / "checks"
    write(d / "a_pass.sh", "echo 'row: a_pass A=1 B=1'\n")
    rc, out = run_checks(tmp, d)
    expect(rc == 0 and "[checks/a_pass]" in out and "row: a_pass A=1 B=1" in out,
           "run_checks: a check added as a file runs and its row reaches the summary (rc %s)" % rc)
    cases = {
        "b_fail.sh": ("echo 'row: B regressed'\nexit 3\n", "checks/b_fail exited 3"),
        "c_crash.sh": ("echo 'row: before crash'\nkill -SEGV $$\n", "checks/c_crash was killed by signal 11"),
        "d_silent.sh": ("echo 'only stderr' >&2\n", "checks/d_silent printed no row"),
    }
    for name, (body, want) in cases.items():
        one = tmp / ("checks-" + name)
        write(one / "a_pass.sh", "echo 'row: a_pass'\n")
        write(one / name, body)
        rc, out = run_checks(tmp, one)
        expect(rc == 1 and ("CHECK FAILURE: " + want) in out and "row: a_pass" in out,
               "run_checks: %s fails the A/B (rc %s, %r)" % (name, rc, want))
    base = tmp / "base-checks"
    write(base / "a_pass.sh", "echo 'row: a_pass'\n")
    write(base / "gone.sh", "echo 'row: gone'\n")
    rc, out = run_checks(tmp, d, base)
    expect(rc == 1 and "missing here: gone.sh" in out,
           "run_checks: a check script the base tree has and this tree lost fails (rc %s)" % rc)
    rc, out = run_checks(tmp, d, tmp / "no-such-dir")
    expect(rc == 0, "run_checks: a base tree with no checks/ folder is fine (rc %s)" % rc)


def test_browser_d(tmp):
    if not shutil.which("node"):
        print("SKIP browser_d: node not found")
        return
    # The same layout and node flags as run.sh: tests/ab/browser_d.js and
    # tests/ab/browser.d/<name>.js, each module preloaded with -r.
    ab = tmp / "ab"
    write(ab / "browser_d.js", (AB / "browser_d.js").read_text())
    d = ab / "browser.d"
    reg = "require('../browser_d.js').add(__filename, %s);\n"
    write(d / "b_good.js", reg % "async t => t.check(t.port === 5, 'port ' + t.port)")
    write(d / "a_throws.js", reg % "async () => { throw new Error('boom'); }")
    write(d / "c_nothing.js", reg % "async () => undefined")
    write(d / "d_nofunc.js", reg % "'not a function'")
    write(d / "e_syntax.js", "require('../browser_d.js').add(__filename,\n")
    # Run from ab/, so ./browser_d.js is the copy the modules add to.
    main_js = ("const m = require('./browser_d.js');"
               "const check = (ok, value) => ({ok: !!ok, value: String(value)});"
               "m.runAll(m.list(), {check, port: 5}).then(r => console.log(JSON.stringify(r)));")

    def node(names):
        pre = [a for n in names for a in ("-r", str(d / n))]
        return subprocess.run(["node"] + pre + ["-e", main_js], cwd=str(ab), capture_output=True, text=True, timeout=60)

    r = node(["d_nofunc.js", "c_nothing.js", "b_good.js", "a_throws.js"])
    try:
        got = json.loads(r.stdout)
    except ValueError:
        got = {}
    expect(list(got) == ["a_throws", "b_good", "c_nothing", "d_nofunc"],
           "browser_d: every preloaded module runs, sorted by file name: %s" % list(got))
    expect(got.get("b_good", {}).get("ok") is True, "browser_d: a good module passes with the helpers it is given")
    for name, want in (("a_throws", "threw: boom"), ("c_nothing", "no check result"),
                       ("d_nofunc", "needs a check function")):
        c = got.get(name) or {}
        expect(c.get("ok") is False and want in c.get("value", ""),
               "browser_d: %s fails with %r: %s" % (name, want, c))
    r = node(["b_good.js", "e_syntax.js"])
    expect(r.returncode != 0 and not r.stdout.strip(),
           "browser_d: a module that does not load stops node, so the browser checks break (rc %s)" % r.returncode)
    r = node([])
    expect(r.stdout.strip() == "{}", "browser_d: no modules means no extra checks")


def side(path, browser_checks):
    write(path / "http.json", json.dumps({"checks": {n: {"ok": True, "value": "ok"} for n in
                                                     ("health", "session_list", "open_session")}}))
    write(path / "vendor.json", json.dumps({"checks": {"vendor_listed": {"ok": True, "value": "ok"}}}))
    write(path / "browser.json", json.dumps({"browser": "1.0", "checks": {
        n: {"ok": True, "value": "ok"} for n in browser_checks}}))


def summary(tmp, btree, atree, browser_checks):
    a, b = tmp / "sides" / "A", tmp / "sides" / "B"
    shutil.rmtree(tmp / "sides", ignore_errors=True)
    side(a, browser_checks)
    side(b, browser_checks)
    env = dict(os.environ)
    env.pop("AB_A_DIR", None)
    if atree:
        env["AB_A_DIR"] = str(atree)
    r = subprocess.run([sys.executable, "-B", str(btree / "tests" / "ab" / "ab.py"), "summary", str(a), str(b)],
                       capture_output=True, text=True, timeout=60, env=env)
    return r.returncode, r.stdout


def test_ab_py(tmp):
    sys.path.insert(0, str(AB))
    import ab
    builtin = [n for n in ab.ORDER if n not in ab.HTTP_CHECKS]
    btree, atree = tmp / "btree", tmp / "atree"
    write(btree / "tests" / "ab" / "ab.py", (AB / "ab.py").read_text())
    write(btree / "tests" / "ab" / "browser.d" / "new_check.js", "// stand-in module\n")
    (atree / "tests" / "ab" / "browser.d").mkdir(parents=True)
    rc, out = summary(tmp, btree, atree, builtin + ["new_check"])
    rows = [line.split()[0] for line in out.splitlines() if line.split() and line.split()[0] in builtin + ["new_check"]]
    at = rows.index("resume_box") if "resume_box" in rows else len(rows)
    expect(rc == 0 and rows[at - 1:at + 1] == ["new_check", "resume_box"],
           "ab.py: a browser.d check shows as a row just before resume_box (rc %s, rows %s)" % (rc, rows[at - 1:]))
    rc, out = summary(tmp, btree, atree, builtin)
    expect(rc == 1 and "B did not report browser check(s): new_check" in out,
           "ab.py: a browser.d module B did not run fails the A/B (rc %s)" % rc)
    write(atree / "tests" / "ab" / "browser.d" / "old_check.js", "// in the base tree only\n")
    rc, out = summary(tmp, btree, atree, builtin + ["new_check"])
    expect(rc == 1 and "B did not report browser check(s): old_check" in out,
           "ab.py: a browser.d module deleted from B but in the base tree fails the A/B (rc %s)" % rc)


def test_browser_d_clash(tmp):
    if not shutil.which("node"):
        print("SKIP browser_d clash: node not found")
        return
    # A browser.d module named like a built-in check would overwrite that
    # check's result in browser.js (Object.assign), hiding a regression.
    clash_dir = tmp / "clash"
    write(clash_dir / "browser_d.js", (AB / "browser_d.js").read_text())
    write(clash_dir / "clash_check.js", "const m = require('./browser_d.js');\n"
          "console.log(JSON.stringify(m.clashes([{name: 'terminal'}, {name: 'new_check'}], ['terminal', 'resume_box'])));\n")
    r = subprocess.run(["node", "clash_check.js"], cwd=str(clash_dir), capture_output=True, text=True, timeout=30)
    expect(r.returncode == 0 and r.stdout.strip() == '["terminal"]',
           "browser_d: a module named like a built-in check is reported as a clash (rc %s, %r)" % (r.returncode, r.stdout.strip() or r.stderr.strip()[:120]))


def test_http_name_clash(tmp):
    """A browser.d/health.js must not replace the real HTTP health result."""
    r = subprocess.run([sys.executable, "-B", str(AB / "ab.py"), "builtin"],
                       capture_output=True, text=True, timeout=30)
    try:
        builtin = json.loads(r.stdout)
    except ValueError:
        builtin = []
    expect("health" in builtin and "vendor_listed" in builtin and "resume_box" in builtin,
           "ab.py builtin lists ORDER and HTTP_CHECKS (rc %s, %s)" % (r.returncode, builtin))
    if shutil.which("node"):
        # run.sh's layout and flags: health.js preloaded, the list from ab.py builtin.
        ab = tmp / "clash-http"
        write(ab / "browser_d.js", (AB / "browser_d.js").read_text())
        write(ab / "browser.d" / "health.js",
              "require('../browser_d.js').add(__filename, async t => t.check(true, 'from a module'));\n")
        js = ("const m = require('./browser_d.js');"
              "let r; try { r = m.clashes(m.list(), m.reserved()); } catch (e) { r = 'threw: ' + e.message; }"
              "console.log(JSON.stringify(r));")
        env = dict(os.environ, AB_BUILTIN_CHECKS=r.stdout.strip())
        n = subprocess.run(["node", "-r", str(ab / "browser.d" / "health.js"), "-e", js], cwd=str(ab),
                           capture_output=True, text=True, timeout=30, env=env)
        expect(n.stdout.strip() == '["health"]',
               "browser_d: a browser.d/health.js module counts as a clash (%r)" % (n.stdout.strip() or n.stderr.strip()[:120]))
        env.pop("AB_BUILTIN_CHECKS")
        n = subprocess.run(["node", "-e", js], cwd=str(ab), capture_output=True, text=True, timeout=30, env=env)
        expect("threw: AB_BUILTIN_CHECKS" in n.stdout,
               "browser_d: no built-in list from run.sh fails closed (%r)" % n.stdout.strip())
    else:
        print("SKIP browser_d http clash: node not found")
    # ab.py: a browser result named health cannot hide B's failing HTTP health.
    sys.path.insert(0, str(AB))
    import ab
    sides = tmp / "http-clash"
    for label in ("A", "B"):
        side(sides / label, [n for n in ab.ORDER if n not in ab.HTTP_CHECKS])
    write(sides / "B" / "http.json", json.dumps({"checks": {
        "health": {"ok": False, "value": "500"}, "session_list": {"ok": True, "value": "ok"},
        "open_session": {"ok": True, "value": "ok"}}}))
    br = json.loads((sides / "B" / "browser.json").read_text())
    br["checks"]["health"] = {"ok": True, "value": "from browser.d/health.js"}
    write(sides / "B" / "browser.json", json.dumps(br))
    env = dict(os.environ)
    env.pop("AB_A_DIR", None)
    r = subprocess.run([sys.executable, "-B", str(AB / "ab.py"), "summary", str(sides / "A"), str(sides / "B")],
                       capture_output=True, text=True, timeout=60, env=env)
    expect(r.returncode == 1 and "REGRESSION: passes on A, fails on B: health" in r.stdout
           and "reused HTTP check name(s): health" in r.stdout,
           "ab.py: a browser result named health does not hide B's HTTP health failure (rc %s)" % r.returncode)


def test_agents_md():
    try:
        text = (ROOT / "AGENTS.md").read_text()
    except OSError:
        text = ""
    expect("tests/ab/checks/" in text and "tests/ab/browser.d/" in text,
           "AGENTS.md says to add an A/B check as a new file in tests/ab/checks/ or tests/ab/browser.d/")


def test_slow_start_summary_once():
    # run.sh summarized $AB_OUT/slow-*.json before checks/slow_start.sh wrote
    # them, so a reused AB_OUT with an old A-pass/B-fail set rc=1 on a passing run.
    text = (AB / "run.sh").read_text()
    expect('slow_start.py" summary' not in text,
           "run.sh: no slow-start summary outside checks/slow_start.sh (it writes the JSON it reads)")


def main():
    tmp = Path(tempfile.mkdtemp(prefix="ab-hooks-"))
    try:
        test_run_checks(tmp)
        test_browser_d(tmp)
        test_browser_d_clash(tmp)
        test_http_name_clash(tmp)
        test_ab_py(tmp)
        test_agents_md()
        test_slow_start_summary_once()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if failures:
        print("FAIL: %d check(s): %s" % (len(failures), "; ".join(failures)))
        return 1
    print("PASS: ab hooks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
