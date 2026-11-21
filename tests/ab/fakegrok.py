# Stand-in for grok's TUI in the A/B run (never the real grok, no model).
# It sets the DEC modes grok sets (1049, 1000, 1002, 1003, 1006, 1015, 2004,
# 25l, 2026 frames), shows a 400-line scrollable view with a right-edge
# scroll bar that takes SGR wheel, press and drag, and keeps a status line
# on the last row: "off=<offset>/<max> size=<rows>x<cols> n=<wheel reports
# handled> last=<event> $".  The "$" ends it: the page can see a frame half
# drawn, and a reading without it is not one.  A click on row 1, column 1
# opens a link as grok does (#34): last=linkRc<$BROWSER's exit code> or
# last=noDisplay.  The key "k" alone asks for another link the same way, as
# a process under grok could with no click: last=keyRc<code> or noDisplay.
# The key "s" starts a background ask every 0.55 s for spam1..spam6 (last=spam).
import fcntl
import os
import re
import select
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
import tty

LINES = ["line %03d  %s" % (i, "lorem ipsum dolor sit amet " * 2) for i in range(400)]
out = sys.stdout.buffer
fd = sys.stdin.fileno()
tty.setraw(fd)
off = None
last = "none"
wheels = 0
drag = False


def size():
    r, c, _, _ = struct.unpack("HHHH", fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8))
    return r or 24, c or 80


def draw():
    global off
    rows, cols = size()
    view = rows - 1
    maxoff = max(0, len(LINES) - view)
    if off is None:
        off = maxoff
    off = max(0, min(maxoff, off))
    thumb_h = max(1, view * view // len(LINES))
    thumb_y = (off * (view - thumb_h)) // maxoff if maxoff else 0
    s = [b"\x1b[?2026h\x1b[H"]
    for y in range(view):
        txt = LINES[off + y] if off + y < len(LINES) else ""
        bar = "█" if thumb_y <= y < thumb_y + thumb_h else "│"
        s.append(("\x1b[%d;1H\x1b[2K%s\x1b[%d;%dH%s" % (y + 1, txt[:cols - 1], y + 1, cols, bar)).encode())
    status = " off=%d/%d size=%dx%d n=%d last=%s $" % (off, maxoff, rows, cols, wheels, last)
    s.append(("\x1b[%d;1H\x1b[2K\x1b[7m%s\x1b[0m" % (rows, status[:cols - 1])).encode())
    s.append(b"\x1b[?2026l")
    out.write(b"".join(s))
    out.flush()
    return cols, view, maxoff


def bar_to_off(y, view, maxoff):
    return int(round((y - 1) * maxoff / max(1, view - 1)))


out.write(b"\x1b[?1049h\x1b[?25l\x1b[?1000h\x1b[?1002h\x1b[?1003h\x1b[?1006h\x1b[?1015h\x1b[?2004h")
out.flush()
# A resize only wakes the main loop, which redraws.  Drawing inside the
# handler let a second SIGWINCH start a frame inside the first one.
wake_r, wake_w = os.pipe()
os.set_blocking(wake_r, False)
os.set_blocking(wake_w, False)
signal.set_wakeup_fd(wake_w)
signal.signal(signal.SIGWINCH, lambda *a: None)
LINK = "https://example.com/ab-link"
# Asked for on the key "k", as a process under grok could do with no click.
KEY_LINK = "https://example.com/ab-key"


def open_link(link=LINK, label="link"):
    # What grok does on a click on a link (#34): run $BROWSER with the URL,
    # or with no BROWSER and no display show "Can't open links without a display".
    browser = os.environ.get("BROWSER", "").split()
    if not browser:
        return "noDisplay"
    args = [a.replace("%s", link) for a in browser]
    if link not in args:
        args.append(link)
    rc = subprocess.call(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return "%sRc%d" % (label, rc)


def spam():
    for i in range(1, 7):
        open_link("https://example.com/spam%d" % i, "spam")
        time.sleep(0.55)


cols, view, maxoff = draw()
SGR = re.compile(rb"\x1b\[<(\d+);(\d+);(\d+)([Mm])")
while True:
    try:
        ready, _, _ = select.select([fd, wake_r], [], [], 1)
        if wake_r in ready:
            while True:
                try:
                    if not os.read(wake_r, 512):
                        break
                except BlockingIOError:
                    break
            cols, view, maxoff = draw()
        if fd not in ready:
            continue
        d = os.read(fd, 4096)
    except InterruptedError:
        continue
    except OSError:
        break
    if not d:
        break
    if b"\x03" in d:
        out.write(b"\x1b[?1049l")
        out.flush()
        sys.exit(0)
    for m in SGR.finditer(d):
        b, x, y, fin = int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4)
        if b & 64:
            wheels += 1
            off += 3 if b & 1 else -3
            last = "wheel%s" % ("Dn" if b & 1 else "Up")
        elif (b & 3) == 0 and fin == b"M" and not (b & 32):
            if x >= cols:
                drag = True
                off = bar_to_off(y, view, maxoff)
                last = "barPress"
            else:
                last = "press"
        elif (b & 32) and drag:
            off = bar_to_off(y, view, maxoff)
            last = "barDrag"
        elif fin == b"m":
            drag = False
            last = "release"
            if x == 1 and y == 1:
                last = open_link()
    rest = SGR.sub(b"", d)
    if rest == b"k":
        last = open_link(KEY_LINK, "key")
    if rest == b"s":
        # Never draws: only the main loop writes to the terminal.
        threading.Thread(target=spam, daemon=True).start()
        last = "spam"
    off -=rest.count(b"\x1b[5~") * view + rest.count(b"\x1b[A") + rest.count(b"\x1bOA")
    off += rest.count(b"\x1b[6~") * view + rest.count(b"\x1b[B") + rest.count(b"\x1bOB")
    cols, view, maxoff = draw()
