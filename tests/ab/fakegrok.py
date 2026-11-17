# Stand-in for grok's TUI in the A/B run (never the real grok, no model).
# It sets the DEC modes grok sets (1049, 1000, 1002, 1003, 1006, 1015, 2004,
# 25l, 2026 frames), shows a 400-line scrollable view with a right-edge
# scroll bar that takes SGR wheel, press and drag, and keeps a status line
# on the last row: "off=<offset>/<max> size=<rows>x<cols> last=<event>".
import fcntl
import os
import re
import select
import signal
import struct
import sys
import termios
import tty

LINES = ["line %03d  %s" % (i, "lorem ipsum dolor sit amet " * 2) for i in range(400)]
out = sys.stdout.buffer
fd = sys.stdin.fileno()
tty.setraw(fd)
off = None
last = "none"
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
    s.append(("\x1b[%d;1H\x1b[2K\x1b[7m off=%d/%d size=%dx%d last=%s \x1b[0m"
              % (rows, off, maxoff, rows, cols, last)).encode())
    s.append(b"\x1b[?2026l")
    out.write(b"".join(s))
    out.flush()
    return cols, view, maxoff


def bar_to_off(y, view, maxoff):
    return int(round((y - 1) * maxoff / max(1, view - 1)))


out.write(b"\x1b[?1049h\x1b[?25l\x1b[?1000h\x1b[?1002h\x1b[?1003h\x1b[?1006h\x1b[?1015h\x1b[?2004h")
out.flush()
signal.signal(signal.SIGWINCH, lambda *a: draw())
cols, view, maxoff = draw()
SGR = re.compile(rb"\x1b\[<(\d+);(\d+);(\d+)([Mm])")
while True:
    try:
        ready, _, _ = select.select([fd], [], [], 1)
        if not ready:
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
    rest = SGR.sub(b"", d)
    off -= rest.count(b"\x1b[5~") * view + rest.count(b"\x1b[A") + rest.count(b"\x1bOA")
    off += rest.count(b"\x1b[6~") * view + rest.count(b"\x1b[B") + rest.count(b"\x1bOB")
    cols, view, maxoff = draw()
