"""ERGOSPHERE_CONFIG sets the config file path; unset keeps the old default.

Run: python3 -B tests/config_path_test.py
Stdlib only, no network, no grok, no server.
"""
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_module():
    spec = importlib.util.spec_from_file_location("ergosphere", ROOT / "ergosphere.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    erg = load_module()
    failures = []

    os.environ.pop("ERGOSPHERE_CONFIG", None)
    default = ROOT / "ergosphere.json"
    if erg.config_path() != default:
        failures.append("unset: expected %s, got %s" % (default, erg.config_path()))

    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "custom.json"
        target.write_text(json.dumps({"port": 18555, "agents": {}}), encoding="utf-8")
        os.environ["ERGOSPHERE_CONFIG"] = str(target)
        try:
            if erg.config_path() != target:
                # Stop here so a failing run never writes the real config.
                failures.append("set: expected %s, got %s" % (target, erg.config_path()))
                raise LookupError
            config = erg.load_config(migrate=False)
            if config.get("port") != 18555:
                failures.append("load_config did not read the ERGOSPHERE_CONFIG file: %r" % config.get("port"))
            config["port"] = 18556
            erg.write_config(config)
            saved = json.loads(target.read_text(encoding="utf-8"))
            if saved.get("port") != 18556:
                failures.append("write_config did not write the ERGOSPHERE_CONFIG file")
            if not (Path(tmp) / "custom.json.bak").exists():
                failures.append("write_config did not keep a .bak next to the ERGOSPHERE_CONFIG file")
        except LookupError:
            pass
        finally:
            os.environ.pop("ERGOSPHERE_CONFIG", None)

    for line in failures:
        print("FAIL " + line)
    if failures:
        return 1
    print("PASS config path honours ERGOSPHERE_CONFIG")
    return 0


if __name__ == "__main__":
    sys.exit(main())
