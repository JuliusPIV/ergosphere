# Agent notes

## A/B checks
Add an A/B check as a new file in `tests/ab/checks/` or `tests/ab/browser.d/`; never edit `run.sh`, the `ORDER` line in `tests/ab/ab.py` or `tests/ab/browser.js` for a new check.
- `tests/ab/checks/<name>.sh`: run with bash after run.sh's own sections, with `AB_A_DIR`, `AB_B_DIR`, `AB_PORT_A`, `AB_PORT_B`, `AB_OUT` set.  Print summary rows; exit 0 when B passes.  Worked example: `checks/slow_start.sh`.
- `tests/ab/browser.d/<name>.js`: one page check, `require('../browser_d.js').add(__filename, async t => t.check(ok, value))`.  `browser_d.js` lists the helpers in `t`.
- A check that fails, crashes or prints nothing fails the A/B, and so does a check file that `main` has and the PR does not.
