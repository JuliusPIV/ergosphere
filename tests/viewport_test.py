"""Every page declares a device-width viewport so phones do not render it zoomed out.

Run: python3 -B tests/viewport_test.py
Stdlib only, no network, no grok, no server.
"""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
META = '<meta name="viewport" content="width=device-width, initial-scale=1">'


def load_module():
    spec = importlib.util.spec_from_file_location("ergosphere", ROOT / "ergosphere.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    erg = load_module()
    pages = {
        "session list": erg.dashboard_page([], {}, {}),
        "settings": erg.settings_page({}),
        "chat pick": erg.chat_pick_page(),
        "chat": erg.chat_page(),
    }
    failed = 0
    for name, page in pages.items():
        if META in page:
            print("PASS %s page has the viewport meta" % name)
        else:
            print("FAIL %s page has no viewport meta" % name)
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
