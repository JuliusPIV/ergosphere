"""The chat page turns off Edge writing hints on the xterm helper textarea.

Edge draws a blue hint dot on the caret of the focused text field, and xterm
moves its hidden textarea to the terminal cursor, so the dot follows it.

Run: python3 -B tests/writing_hints_test.py
Stdlib only, no network, no grok, no server.
"""
import importlib.util
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_module():
    spec = importlib.util.spec_from_file_location("ergosphere", ROOT / "ergosphere.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    erg = load_module()
    page = erg.chat_page()
    pattern = re.compile(
        r'term\.open\(document\.getElementById\("term"\)\);.*?'
        r'term\.textarea\.setAttribute\("writingsuggestions", "false"\)',
        re.S,
    )
    if not pattern.search(page):
        print('FAIL chat page does not set writingsuggestions="false" on term.textarea after term.open')
        return 1
    print('PASS chat page sets writingsuggestions="false" on the terminal textarea')
    return 0


if __name__ == "__main__":
    sys.exit(main())
