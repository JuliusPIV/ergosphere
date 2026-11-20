"""The vendored xterm.js files are the exact package versions package-lock.json names.

Scanners (osv-scanner, Dependabot) only see vendor/ through package.json and
package-lock.json, so a vendor file that drifts from the listed version hides it.
KNOWN holds, per package version, the npm tarball's integrity and the sha256 of
each vendored file as cut from that tarball (vendor/xterm.js is lib/xterm.js
without its last line, "//# sourceMappingURL=xterm.js.map").  To bump a version:
copy the new files in, update both package files, and add its row here from the
official tarball (npm pack <name>@<version>).

Run: python3 -B tests/vendor_manifest_test.py
Stdlib only, no network.
"""
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

KNOWN = {
    ("@xterm/xterm", "5.5.0"): {
        "integrity": "sha512-hqJHYaQb5OptNunnyAnkHyM8aCjZ1MEIDTQu1iIbbTD/xops91NB5yq1ZK/dC2JDbVWtF23zUtl9JE2NqwT87A==",
        "files": {
            "vendor/xterm.js": "7a88e46a58847d3f17109e3d48cc8aa056a01997d00ceb76035f63370712d858",
            "vendor/xterm.css": "ba8e6985669488981ccf40c0cefe3aba80722cb6c92de7ad628b0bd717faf2b6",
        },
    },
    ("@xterm/addon-fit", "0.10.0"): {
        "integrity": "sha512-UFYkDm4HUahf2lnEyHvio51TNGiLK66mqP2JoATy7hRZeXaGMRDr00JiSF7m63vR5WKATF605yEggJKsw0JpMQ==",
        "files": {
            "vendor/addon-fit.js": "bdaefa370b1bfc42ee88d46fe6072400902a4d4b2d45cd93438dda9b23c97089",
        },
    },
}
VENDORED = ["vendor/xterm.js", "vendor/xterm.css", "vendor/addon-fit.js"]


def check():
    """Yield (ok, message) per check."""
    try:
        deps = json.loads((ROOT / "package.json").read_text())["dependencies"]
        lock = json.loads((ROOT / "package-lock.json").read_text())["packages"]
    except (OSError, ValueError, KeyError) as e:
        yield False, "package.json / package-lock.json missing or unreadable: %s" % e
        return
    yield lock.get("", {}).get("dependencies") == deps, "lockfile root lists package.json dependencies %s" % deps
    covered = set()
    for name, version in sorted(deps.items()):
        entry = lock.get("node_modules/" + name) or {}
        known = KNOWN.get((name, version))
        yield known is not None, "%s@%s has a known row" % (name, version)
        if known is None:
            continue
        yield entry.get("version") == version, "lockfile pins %s at %s (has %s)" % (name, version, entry.get("version"))
        yield entry.get("integrity") == known["integrity"], "lockfile integrity for %s@%s is the npm tarball's" % (name, version)
        yield "registry.npmjs.org" in (entry.get("resolved") or ""), "lockfile resolves %s from the npm registry" % name
        for path, want in known["files"].items():
            covered.add(path)
            try:
                got = hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
            except OSError as e:
                got = "unreadable (%s)" % e
            yield got == want, "%s is %s@%s (sha256 %s)" % (path, name, version, got)
    for path in VENDORED:
        yield path in covered, "%s is described by a listed package" % path


def main():
    failed = 0
    for ok, msg in check():
        print("%s %s" % ("PASS" if ok else "FAIL", msg))
        failed += not ok
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
