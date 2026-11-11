#!/usr/bin/env python3
# Copyright (C) 2026 Julius Perkins
# Licensed under the GNU Affero General Public License v3.0 only.
# SPDX-License-Identifier: AGPL-3.0-only
"""Browser terminal over an interactive grok TUI.

/ is the session list. /chat is one xterm on a PTY. The PTY stays up when
the socket closes so a second window can attach to the same grok process.
Bytes both ways are a WebSocket. There is no grok agent serve child.
"""

import argparse
import base64
import copy
from datetime import datetime, timedelta, timezone
import errno
import hashlib
import hmac
import html
import ipaddress
import json
import os
import pty
import queue
import re
import select
import shutil
import signal
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import uuid
import fcntl
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

VERSION = "0.1.0"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9241
SESSION_CAP = 50
OLDER_WINDOW = 20
OLDER_STEP = 50
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
FRAME_MAX = 4 * 1024 * 1024
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
EFFORT_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
# grok models rows are "* id (default)" or "- id". Efforts are whatever
# level words that row actually prints. No efforts means the /effort four.
DEFAULT_EFFORTS = ("low", "medium", "high", "xhigh")
_MODEL_ROW_RE = re.compile(r"^[ \t]*([*-])[ \t]+(\S+)[ \t]*(.*)$")
_LISTED_EFFORT_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_-])(minimal|xhigh|medium|none|high|low|max)(?![A-Za-z0-9_-])"
)
SID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
# Session directory names are 8-4-4-4-12 hexadecimal digits.
SID_TOKEN_RE = re.compile(
    r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$"
)
VENDOR_FILES = {
    "/vendor/xterm.js": ("xterm.js", "text/javascript; charset=utf-8"),
    "/vendor/xterm.css": ("xterm.css", "text/css; charset=utf-8"),
    "/vendor/addon-fit.js": ("addon-fit.js", "text/javascript; charset=utf-8"),
}

# One live grok per CLI session id. Closing a browser socket does not remove it.
LIVE = {}
LIVE_LOCK = threading.Lock()
STOP = threading.Event()
# Set once a restart is accepted. No new grok starts after that.
RESTART = threading.Event()
SCREEN_CAP = 2 * 1024 * 1024
FRAME_CHUNK = 64 * 1024
# grok coalesces SIGWINCH. Two resizes closer than this read as one, and the
# same final size does not repaint.
REPAINT_PAUSE = 0.25
# xterm.js 5.5.0 applies these DEC private modes and then sends the drag to
# the PTY. grok treats that as its own copy. Drop set and reset so the
# browser keeps the selection. 1004 and 2004 stay. A click that never
# changes cell is synthesized later as the SGR press and release xterm
# would have sent. Tracking stays off.
MOUSE_DEC_MODES = frozenset((1000, 1002, 1003, 1005, 1006, 1015))
# Once the replay buffer is trimmed, the head that set these modes is gone.
# Without them a reattached xterm draws on the main screen, shows the cursor,
# and pastes without bracketing, so a multi-line paste submits line by line.
# 1049 first: switching screens resets the cursor and must lead.
REPLAY_DEC_MODES = (1049, 25, 1004, 2004)
DEC_MODE_RE = re.compile(rb"\x1b\[\?([0-9;]+)([hl])")
# grok wraps every frame in synchronized output. A trim that starts here
# never begins inside an escape sequence.
FRAME_START = b"\x1b[?2026h"


def strip_mouse_tracking(data, hold=b""):
    """Remove DEC mouse-tracking set/reset. hold is an unfinished ESC [ ? prefix."""
    raw = hold + (data or b"")
    out = bytearray()
    i = 0
    n = len(raw)
    while i < n:
        if raw[i] != 0x1B:
            out.append(raw[i])
            i += 1
            continue
        if i + 2 >= n or raw[i + 1] != 0x5B or raw[i + 2] != 0x3F:
            if n - i < 3 and raw[i:i + 2] in (b"\x1b", b"\x1b["):
                return bytes(out), raw[i:]
            out.append(raw[i])
            i += 1
            continue
        j = i + 3
        params = []
        cur = None
        final = None
        while j < n:
            b = raw[j]
            if 48 <= b <= 57:
                if cur is None:
                    cur = 0
                cur = cur * 10 + (b - 48)
                if cur > 9999:
                    cur = None
                    break
                j += 1
                continue
            if b == 0x3B and cur is not None:
                params.append(cur)
                cur = None
                j += 1
                continue
            if b in (0x68, 0x6C) and cur is not None:
                params.append(cur)
                final = b
                j += 1
            break
        if final is None:
            tail = raw[i + 3:]
            if j >= n and (n - i) <= 48 and all(c in b"0123456789;" for c in tail):
                return bytes(out), raw[i:]
            out.append(raw[i])
            i += 1
            continue
        kept = [p for p in params if p not in MOUSE_DEC_MODES]
        if kept:
            body = b";".join(str(p).encode("ascii") for p in kept)
            out.extend(b"\x1b[?" + body + bytes((final,)))
        i = j
    return bytes(out), b""


def agents_root():
    raw = os.environ.get("GROK_AGENTS_DIR") or os.path.expanduser("~/.grok/agents")
    return Path(raw).expanduser().resolve()


def sessions_root():
    return Path(os.path.expanduser("~/.grok/sessions"))


def frontmatter_name(path):
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    lines = raw.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    try:
        end = lines[1:].index("---") + 1
    except ValueError:
        return None
    name = None
    for line in lines[1:end]:
        if not line.startswith("name:"):
            continue
        value = line[5:].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1].strip()
        name = value
    if not name or not NAME_RE.match(name):
        return None
    return name


def list_agents():
    root = agents_root()
    found = {}
    if not root.is_dir():
        return []
    try:
        entries = sorted(root.iterdir(), key=lambda p: p.name)
    except OSError:
        return []
    for entry in entries:
        # Direct child only. A symlink stays a child; its target may be outside.
        if entry.parent.resolve() != root or not entry.is_file():
            continue
        name = frontmatter_name(entry)
        if not name:
            continue
        found.setdefault(name, []).append(entry)
    names = []
    for name in sorted(found):
        if len(found[name]) != 1:
            continue
        names.append(name)
    return names


def iter_summary_paths():
    root = sessions_root()
    try:
        groups = list(os.scandir(root))
    except OSError:
        return
    for group in groups:
        if not group.is_dir(follow_symlinks=False):
            continue
        try:
            kids = list(os.scandir(group.path))
        except OSError:
            continue
        for kid in kids:
            if not kid.is_dir(follow_symlinks=False):
                continue
            summary = os.path.join(kid.path, "summary.json")
            if os.path.isfile(summary):
                yield summary


def _summary_str(data, key):
    value = data.get(key)
    if isinstance(value, str) and value.strip():
        return value
    return ""


def read_summary(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    info = data.get("info") if isinstance(data.get("info"), dict) else {}
    sid = info.get("id") or Path(path).parent.name
    if not isinstance(sid, str) or not SID_RE.match(sid):
        return None
    # /rename stores the manual name in generated_title and sets
    # title_is_manual. /rename --auto removes both. There is no other
    # text key. A manual name must not fall through to an older summary.
    manual = data.get("title_is_manual") is True
    generated = _summary_str(data, "generated_title")
    if manual:
        title = generated
    else:
        title = (
            generated
            or _summary_str(data, "title")
            or _summary_str(data, "session_summary")
            or _summary_str(data, "last_turn_summary")
        )
    title = " ".join(title.split())[:180]
    cwd = info.get("cwd") if isinstance(info.get("cwd"), str) else ""
    model = data.get("current_model_id") if isinstance(data.get("current_model_id"), str) else ""
    updated = data.get("updated_at") if isinstance(data.get("updated_at"), str) else ""
    return {
        "id": sid,
        "title": title,
        "cwd": cwd,
        "model": model,
        "updated": updated,
    }


def load_sessions(limit=SESSION_CAP):
    rows = []
    total = 0
    for path in iter_summary_paths():
        total += 1
        row = read_summary(path)
        if row:
            rows.append(row)
    rows.sort(key=lambda row: row["updated"], reverse=True)
    if limit is not None:
        rows = rows[:limit]
    return total, rows


def find_session(sid):
    if not isinstance(sid, str) or not SID_RE.match(sid):
        return None
    root = sessions_root()
    try:
        groups = list(os.scandir(root))
    except OSError:
        return None
    for group in groups:
        if not group.is_dir(follow_symlinks=False):
            continue
        summary = os.path.join(group.path, sid, "summary.json")
        if not os.path.isfile(summary):
            continue
        row = read_summary(summary)
        if row and row["id"] == sid:
            return row
    return None


def session_dir_for(sid):
    if not isinstance(sid, str) or not SID_RE.match(sid):
        return None
    root = sessions_root()
    try:
        groups = list(os.scandir(root))
    except OSError:
        return None
    for group in groups:
        if not group.is_dir(follow_symlinks=False):
            continue
        path = os.path.join(group.path, sid)
        if os.path.isdir(path):
            return path
    return None


def session_updates_path(sid):
    folder = session_dir_for(sid)
    if not folder:
        return None
    path = os.path.join(folder, "updates.jsonl")
    if os.path.isfile(path):
        return path
    return None


BUTTON_LABEL = "Quit that CLI and resume here"


def read_proc_cmdline(pid):
    try:
        with open("/proc/%d/cmdline" % int(pid), "rb") as handle:
            return handle.read()
    except (OSError, ValueError):
        return b""


def grok_cmdline(raw):
    args = [part for part in raw.split(b"\0") if part]
    if not args:
        return False
    return os.path.basename(args[0]) == b"grok"


def cmdline_contains_sid(raw, sid):
    # NUL-separated argv, not open files. A path only in /proc/pid/fd does not count.
    # Whole argv element only: a substring would match an unrelated path.
    # grok also takes the id attached: --resume=<id> and -r<id>.
    if not raw or not isinstance(sid, str) or not sid:
        return False
    want = sid.encode("ascii")
    forms = (want, b"--resume=" + want, b"-r" + want)
    return any(part in forms for part in raw.split(b"\0"))


def proc_stat_ppid(pid):
    """ppid from /proc/<pid>/stat. comm can contain spaces and parentheses."""
    try:
        raw = open("/proc/%s/stat" % int(pid), "rb").read()
    except (OSError, ValueError):
        return None
    cut = raw.rfind(b")")
    if cut < 0:
        return None
    fields = raw[cut + 2:].split()
    if len(fields) < 2:
        return None
    try:
        return int(fields[1])
    except ValueError:
        return None


def is_ergosphere_server(pid):
    """True for a python process whose argv runs a script named ergosphere.py."""
    args = [part for part in read_proc_cmdline(pid).split(b"\0") if part]
    if len(args) < 2 or not os.path.basename(args[0]).startswith(b"python"):
        return False
    return any(os.path.basename(arg) == b"ergosphere.py" for arg in args[1:])


def holder_kind(pid):
    """web when any ancestor is a python process running ergosphere.py, else cli.

    A wrapper on PATH can fork, so the real grok's parent is the shell,
    not this server. Walk /proc/<pid>/stat ppid up to init. The walk is
    bounded. A web ancestor is an Ergosphere server, not a terminal to quit.
    """
    try:
        current = int(pid)
    except (TypeError, ValueError):
        return "cli"
    seen = set()
    for _ in range(64):
        if current in seen or current <= 0:
            return "cli"
        seen.add(current)
        ppid = proc_stat_ppid(current)
        if ppid is None or ppid <= 0 or ppid == current or ppid in seen:
            return "cli"
        if is_ergosphere_server(ppid):
            return "web"
        if ppid <= 1:
            return "cli"
        current = ppid
    return "cli"


def holder_can_quit(holder):
    return bool(
        isinstance(holder, dict)
        and holder.get("via") == "cmdline"
        and holder.get("kind") != "web"
    )


def own_grok_pids():
    """PIDs of the grok terminals this server runs.

    Each tracked child, and when that child is a wrapper, the first grok
    under it. The walk stops at any grok and at any ergosphere.py server:
    a grok or an Ergosphere started from a web session's shell holds its
    own session, and Resume must still see it.
    """
    with LIVE_LOCK:
        roots = []
        for live in LIVE.values():
            proc = getattr(live, "proc", None)
            if proc is not None and proc.pid:
                roots.append(proc.pid)
    own = set(roots)
    children = {}
    for child, parent in _proc_ppid_map().items():
        children.setdefault(parent, []).append(child)
    stack = [root for root in roots if not grok_cmdline(read_proc_cmdline(root))]
    seen = set()
    while stack and len(seen) < 4096:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        for child in children.get(pid, ()):
            if is_ergosphere_server(child):
                continue
            if grok_cmdline(read_proc_cmdline(child)):
                own.add(child)
                continue
            stack.append(child)
    return own


def session_holder(sid):
    """Grok that already owns this session, or None.

    via=cmdline: the id is on that grok's command line (Ergosphere resume, or
    `grok -r <id>`). via=openfile: a grok has `<session>/` open and the id
    is not on its command line. kind=web when any ancestor is a python
    process running ergosphere.py, otherwise kind=cli. Only a cmdline cli holder may be
    signalled. This server's own PTY grok, including a wrapper's child, is
    not a holder. Only a UUID-shaped session id is looked up.
    """
    if not isinstance(sid, str) or not SID_TOKEN_RE.match(sid):
        return None
    marker = "/" + sid + "/"
    own = own_grok_pids()
    cmdline_pid = None
    file_pid = None
    try:
        pids = os.listdir("/proc")
    except OSError:
        return None
    for pid_text in pids:
        if not pid_text.isdigit():
            continue
        pid = int(pid_text)
        if pid in own:
            continue
        raw = read_proc_cmdline(pid)
        if not raw or not grok_cmdline(raw):
            continue
        if cmdline_contains_sid(raw, sid):
            cmdline_pid = pid
            break
        if file_pid is not None:
            continue
        fd_dir = "/proc/%s/fd" % pid
        try:
            names = os.listdir(fd_dir)
        except OSError:
            continue
        for name in names:
            try:
                target = os.readlink(os.path.join(fd_dir, name))
            except OSError:
                continue
            if marker in target:
                file_pid = pid
                break
    if cmdline_pid is not None:
        return {"pid": cmdline_pid, "via": "cmdline", "kind": holder_kind(cmdline_pid)}
    if file_pid is not None:
        return {"pid": file_pid, "via": "openfile", "kind": holder_kind(file_pid)}
    return None


def busy_notice(sid, holder):
    pid = holder["pid"]
    if holder.get("kind") == "web":
        return (
            "This session is already open in a web dashboard on this machine. "
            "This page cannot attach to that terminal. "
            "Resume is offered here when that grok exits.\n"
            "session id: %s\n"
            "holder pid: %s\n"
        ) % (sid, pid)
    if holder["via"] == "cmdline":
        return (
            "This session is open in a Grok CLI on this machine. "
            "This page checks once a second and offers Resume when that CLI exits. "
            "A button can quit that CLI and open the session here.\n"
            "session id: %s\n"
            "holder pid: %s\n"
            "%s\n"
        ) % (sid, pid, BUTTON_LABEL)
    return (
        "This session is open in a Grok CLI on this machine. "
        "This page checks once a second and offers Resume when that CLI exits. "
        "The holder was detected from an open file, and its command line "
        "does not contain the session id, so this page will not quit it.\n"
        "session id: %s\n"
        "holder pid: %s\n"
    ) % (sid, pid)


def pid_gone(pid):
    if not os.path.exists("/proc/%d" % pid):
        return True
    # A zombie still has a /proc entry. Its command line is empty, and the
    # holder scan skips that, so the resume is not blocked.
    return read_proc_cmdline(pid) == b""


def release_holder(sid):
    """SIGTERM a cmdline cli holder and wait until that pid is gone.

    Returns (ok, info). Does not signal a web holder, an open-file holder,
    a non-grok process, or a pid whose command line no longer contains the
    session id. A web holder is refused even when the id is on its command line.
    """
    holder = session_holder(sid)
    if not holder:
        return False, {"ok": False, "error": "no holder"}
    pid = holder["pid"]
    if pid <= 1 or pid == os.getpid():
        return False, {"ok": False, "error": "refusing that pid", "pid": pid}
    raw = read_proc_cmdline(pid)
    if holder.get("kind") == "web" and not pid_gone(pid):
        print(
            "take refuse pid=%s sid=%s via=%s kind=web" % (pid, sid, holder["via"]),
            flush=True,
        )
        return False, {
            "ok": False,
            "error": "web session is not quit",
            "pid": pid,
            "via": holder["via"],
            "kind": "web",
        }
    if pid_gone(pid):
        return True, {"ok": True, "pid": pid, "already": "gone"}
    if not grok_cmdline(raw) or not cmdline_contains_sid(raw, sid):
        print("take refuse pid=%s sid=%s via=%s" % (pid, sid, holder["via"]), flush=True)
        return False, {
            "ok": False,
            "error": "command line does not contain the session id",
            "pid": pid,
            "via": holder["via"],
        }
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True, {"ok": True, "pid": pid}
    except (PermissionError, OSError) as exc:
        return False, {"ok": False, "error": "cannot signal that pid: %s" % exc, "pid": pid}
    print("take sigterm pid=%s sid=%s" % (pid, sid), flush=True)
    deadline = time.time() + 12
    while time.time() < deadline:
        if pid_gone(pid) or not cmdline_contains_sid(read_proc_cmdline(pid), sid):
            print("take gone pid=%s sid=%s" % (pid, sid), flush=True)
            return True, {"ok": True, "pid": pid}
        time.sleep(0.1)
    return False, {
        "ok": False,
        "error": "holder still running after SIGTERM",
        "pid": pid,
    }


def apply_session_update(state, upd, owner):
    """Fold one updates.jsonl record into subagent rows.

    Live files have no field named progress. subagent_spawned carries
    description. The line that changes while the child runs is tool_call
    title. A parent tool title counts only when it names that subagent.
    Returns True when the visible row changed.
    """
    if not isinstance(upd, dict):
        return False
    kind = upd.get("sessionUpdate")
    if owner is None and kind == "subagent_spawned":
        sid = upd.get("subagent_id")
        if not isinstance(sid, str) or not sid:
            sid = upd.get("child_session_id")
        if not isinstance(sid, str) or not sid:
            return False
        desc = upd.get("description") if isinstance(upd.get("description"), str) else ""
        child = upd.get("child_session_id") if isinstance(upd.get("child_session_id"), str) else sid
        row = state["rows"].get(sid)
        if row is None:
            state["rows"][sid] = {
                "id": sid,
                "description": desc,
                "status": "running",
                "title": "",
                "child": child,
            }
            state["order"].append(sid)
            return True
        changed = False
        if desc and row["description"] != desc:
            row["description"] = desc
            changed = True
        if child and row.get("child") != child:
            row["child"] = child
            changed = True
        if row.get("status") != "running":
            row["status"] = "running"
            changed = True
        return changed
    if owner is None and kind == "subagent_finished":
        sid = upd.get("subagent_id")
        if not isinstance(sid, str):
            return False
        row = state["rows"].get(sid)
        if row is None:
            return False
        status = upd.get("status") if isinstance(upd.get("status"), str) and upd.get("status") else "finished"
        if row["status"] == status:
            return False
        row["status"] = status
        return True
    if kind not in ("tool_call", "tool_call_update"):
        return False
    title = upd.get("title")
    if not isinstance(title, str) or not title:
        return False
    targets = []
    if owner is not None:
        for row in state["rows"].values():
            if row.get("child") == owner or row.get("id") == owner:
                targets.append(row)
    else:
        for row in state["rows"].values():
            if row.get("status") != "running":
                continue
            named = bool(row.get("id")) and row["id"] in title
            described = bool(row.get("description")) and row["description"] in title
            if named or described:
                targets.append(row)
    changed = False
    for row in targets:
        if row.get("title") != title:
            row["title"] = title
            changed = True
    return changed


CONFIG_NAME = "ergosphere.json"
LEGACY_AGENTS_NAME = "cwd-defaults.json"
# One read-modify-write of ergosphere.json at a time.
CONFIG_LOCK = threading.Lock()


def config_path():
    # ERGOSPHERE_CONFIG lets the code folder stay read-only.
    raw = os.environ.get("ERGOSPHERE_CONFIG")
    if raw:
        return Path(raw).expanduser()
    return Path(__file__).resolve().parent / CONFIG_NAME


def check_agents_map(data, label):
    # Same rules cwd-defaults.json always had: a name and an absolute path.
    if not isinstance(data, dict):
        raise ValueError("%s agents must be a JSON object" % label)
    out = {}
    for key, value in data.items():
        if not isinstance(key, str) or not key:
            raise ValueError("%s has a bad agent name" % label)
        if not isinstance(value, str) or not os.path.isabs(value):
            raise ValueError("%s path for %s must be absolute" % (label, key))
        out[key] = value
    return out


def valid_bind(value):
    if not isinstance(value, str) or not value:
        return False
    if value == "localhost":
        return True
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def valid_port(value):
    return type(value) is int and 1 <= value <= 65535


def check_config(data, label):
    """Validated copy of an ergosphere.json object. Unknown keys are kept."""
    if not isinstance(data, dict):
        raise ValueError("%s must be a JSON object" % label)
    out = dict(data)
    if "bind" in out and not valid_bind(out["bind"]):
        raise ValueError("%s bind must be an IP address or localhost" % label)
    if "port" in out and not valid_port(out["port"]):
        raise ValueError("%s port must be a whole number from 1 to 65535" % label)
    out["agents"] = check_agents_map(out.get("agents", {}), label)
    last = out.get("last", {})
    if not isinstance(last, dict):
        raise ValueError("%s last must be a JSON object" % label)
    model = last.get("model", "")
    effort = last.get("effort", "")
    if not isinstance(model, str) or (model and not MODEL_ID_RE.match(model)):
        raise ValueError("%s last.model is not a model id" % label)
    if not isinstance(effort, str) or (effort and not EFFORT_ID_RE.match(effort)):
        raise ValueError("%s last.effort is not an effort level" % label)
    out["last"] = {"model": model, "effort": effort}
    return out


def read_json_file(path):
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError("%s cannot be read (%s)" % (path, exc.strerror or exc))
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise ValueError("%s is not valid JSON (%s)" % (path, exc))


def atomic_write(path, data):
    # Temp file in the same directory, fsync, rename, then fsync the directory.
    folder = path.parent
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=str(folder))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        dfd = os.open(str(folder), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dfd)
    except OSError:
        pass
    finally:
        os.close(dfd)


def write_config(data):
    """Write ergosphere.json atomically and keep the old content as .bak."""
    path = config_path()
    text = (json.dumps(data, indent=2) + "\n").encode("utf-8")
    try:
        old = path.read_bytes()
    except FileNotFoundError:
        old = None
    if old == text:
        return
    if old is not None:
        atomic_write(path.with_name(path.name + ".bak"), old)
    atomic_write(path, text)
    print("config saved %s" % path, flush=True)


def load_config(migrate=True):
    """ergosphere.json, or agents migrated once from cwd-defaults.json.

    Neither file means defaults and no agents. A bad file raises SystemExit
    naming the file and the problem.
    """
    path = config_path()
    if path.exists():
        try:
            return check_config(read_json_file(path), str(path))
        except ValueError as exc:
            raise SystemExit("bad config: %s" % exc)
    legacy = path.with_name(LEGACY_AGENTS_NAME)
    if not legacy.exists():
        # stderr, so --check output stays the same lines as before.
        print("config: no %s, using defaults and no agents" % CONFIG_NAME, file=sys.stderr, flush=True)
        return check_config({}, str(path))
    try:
        agents = check_agents_map(read_json_file(legacy), str(legacy))
    except ValueError as exc:
        raise SystemExit("bad config: %s" % exc)
    data = check_config({"agents": agents, "last": {"model": "", "effort": ""}}, str(path))
    if migrate:
        try:
            write_config(data)
        except OSError as exc:
            raise SystemExit("cannot write %s (%s)" % (path, exc.strerror or exc))
        print("config: copied %d agents from %s into %s" % (len(agents), legacy, path), flush=True)
    return data


def vendor_dir():
    return Path(__file__).resolve().parent / "vendor"


def require_vendor():
    missing = []
    root = vendor_dir()
    for name, _ctype in VENDOR_FILES.values():
        if not (root / name).is_file():
            missing.append(name)
    if missing:
        raise SystemExit("missing vendor file: %s" % ", ".join(missing))


def read_token_file():
    raw = os.environ.get("ERGOSPHERE_TOKEN")
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_file():
        raise SystemExit("ERGOSPHERE_TOKEN is not a file")
    token = path.read_text(encoding="utf-8").strip()
    if not token or "\n" in token or "\r" in token:
        raise SystemExit("ERGOSPHERE_TOKEN file is empty or not a single line")
    return token


def grok_bin():
    return shutil.which("grok")


def existing_dir(path):
    if not isinstance(path, str) or not path or not os.path.isabs(path):
        return False
    if "\x00" in path or len(path) > 4096:
        return False
    return os.path.isdir(path)


def one_query(qs, key):
    values = qs.get(key)
    if not values:
        return ""
    value = values[0]
    if not isinstance(value, str):
        return ""
    return value


def dim_arg(raw, default):
    if not raw:
        return default
    try:
        n = int(raw)
    except ValueError:
        return default
    if n < 2:
        return 2
    if n > 500:
        return 500
    return n


def new_launch(agent, approval, cwd, cwd_defaults, fallback_cwd):
    # A blank agent starts grok with no --agent flag. Any other name must be
    # one list_agents() returns. A blank agent and a blank cwd is refused
    # and does not use the server process cwd. An agent with a blank cwd
    # uses that agent's default, then the process cwd. The directory must
    # already exist.
    if not isinstance(agent, str):
        agent = ""
    agent = agent.strip()
    if agent and (not NAME_RE.match(agent) or agent not in list_agents()):
        raise ValueError("unknown agent")
    if approval not in ("ask", "auto", "always-approve"):
        raise ValueError("approval must be ask, auto, or always-approve")
    if not isinstance(cwd, str):
        cwd = ""
    cwd = cwd.strip()
    if not cwd and not agent:
        raise ValueError("Choose a working directory.")
    if not cwd:
        mapped = ""
        if isinstance(cwd_defaults, dict):
            mapped = cwd_defaults.get(agent) or ""
        if not isinstance(mapped, str):
            mapped = ""
        cwd = mapped or fallback_cwd
    if not existing_dir(cwd):
        raise ValueError("cwd must be an existing absolute directory")
    return agent, cwd


def grok_argv(agent, approval, cwd, resume, session_id=None, model="", effort=""):
    # grok 1.0.70 rejects --permission-mode ask. The guide calls `default` ask.
    # --always-approve, --permission-mode, --agent, --resume, --session-id,
    # -m/--model, and --reasoning-effort (alias --effort) are in grok --help.
    # --agent-profile is not a flag on this build.
    # A blank agent omits --agent. Resume never passes it.
    # A blank model omits -m. A blank effort omits --reasoning-effort.
    binary = grok_bin()
    if not binary:
        raise RuntimeError("grok not on PATH")
    cmd = [binary, "--cwd", cwd]
    if resume:
        # Grok otherwise reapplies the account default, which can escalate Ask.
        cmd.extend(["--resume", resume, "--permission-mode", "default"])
        return cmd
    if session_id:
        cmd.extend(["--session-id", session_id])
    if agent:
        cmd.extend(["--agent", agent])
    if model:
        cmd.extend(["-m", model])
    if effort:
        cmd.extend(["--reasoning-effort", effort])
    if approval == "always-approve":
        cmd.append("--always-approve")
    elif approval == "auto":
        cmd.extend(["--permission-mode", "auto"])
    elif approval == "ask":
        cmd.extend(["--permission-mode", "default"])
    else:
        raise ValueError("approval must be ask, auto, or always-approve")
    return cmd


def parse_grok_models(text):
    # `grok models` prints a bullet list and exits. A row that also names
    # effort levels keeps those. A row that does not leaves efforts empty.
    models = []
    seen = set()
    if not isinstance(text, str):
        return models
    for raw in text.splitlines():
        match = _MODEL_ROW_RE.match(raw)
        if not match:
            continue
        model_id = match.group(2)
        if not MODEL_ID_RE.match(model_id) or model_id in seen:
            continue
        seen.add(model_id)
        rest = re.sub(r"\(default\)", " ", match.group(3) or "", flags=re.IGNORECASE)
        efforts = []
        for word in _LISTED_EFFORT_RE.findall(rest):
            level = word.lower()
            if level not in efforts:
                efforts.append(level)
        models.append({"id": model_id, "efforts": efforts})
    return models


# One successful `grok models` run serves every request for MODELS_TTL
# seconds. The lock makes concurrent requests wait for that one run.
MODELS_TTL = 60
_MODELS_LOCK = threading.Lock()
_MODELS_CACHE = {"at": 0.0, "value": None}


def grok_models_list():
    with _MODELS_LOCK:
        now = time.monotonic()
        cached = _MODELS_CACHE["value"]
        if cached is not None and now - _MODELS_CACHE["at"] < MODELS_TTL:
            return cached
        value = run_grok_models()
        # Errors are not cached: a fixed login must show on the next request.
        if not value[1]:
            _MODELS_CACHE["at"] = time.monotonic()
            _MODELS_CACHE["value"] = value
        return value


def run_grok_models():
    # `grok models` lists models and exits. It is not a TUI. 15s then stop.
    binary = grok_bin()
    if not binary:
        return [], "grok not on PATH"
    try:
        proc = subprocess.run(
            [binary, "models"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return [], "grok models timed out"
    except OSError:
        return [], "grok models failed"
    out = proc.stdout.decode("utf-8", "replace") if proc.stdout else ""
    err = proc.stderr.decode("utf-8", "replace") if proc.stderr else ""
    if proc.returncode != 0:
        line = ""
        for raw in (err or out).splitlines():
            raw = raw.strip()
            if raw:
                line = raw
                break
        if not line:
            line = "grok models failed"
        return [], line[:160]
    models = parse_grok_models(out)
    if not models:
        return [], "grok models returned no models"
    return models, ""


def effort_choices(model, models):
    if model:
        for row in models:
            levels = row.get("efforts") or []
            if row.get("id") == model and levels:
                return tuple(levels)
    return DEFAULT_EFFORTS


def validate_model_effort(model, effort, models, err):
    if not isinstance(model, str):
        model = ""
    if not isinstance(effort, str):
        effort = ""
    model = model.strip()
    effort = effort.strip()
    if model and not MODEL_ID_RE.match(model):
        raise ValueError("unknown model")
    if effort and not EFFORT_ID_RE.match(effort):
        raise ValueError("unknown effort")
    if err:
        if model:
            raise ValueError("unknown model")
        allowed = DEFAULT_EFFORTS
    else:
        ids = []
        for row in models:
            mid = row.get("id")
            if isinstance(mid, str):
                ids.append(mid)
        if model and model not in ids:
            raise ValueError("unknown model")
        allowed = effort_choices(model, models)
    if effort and effort not in allowed:
        raise ValueError("unknown effort")
    return model, effort


def checked_model_effort(model, effort):
    models, err = grok_models_list()
    return validate_model_effort(model, effort, models, err)


def kill_proc(proc):
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.terminate()
        except (ProcessLookupError, PermissionError, OSError):
            return
    try:
        proc.wait(timeout=3)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass


def kill_all():
    with LIVE_LOCK:
        lives = list(LIVE.values())
        LIVE.clear()
    for live in lives:
        live.shutdown()


def spawn_grok(cmd, cwd, cols, rows):
    master, slave = pty.openpty()
    winsz = struct.pack("HHHH", rows, cols, 0, 0)
    try:
        fcntl.ioctl(slave, termios.TIOCSWINSZ, winsz)
        env = os.environ.copy()
        # Ergosphere is often started inside a grok session. Inheriting those
        # would nest this TUI in the parent's session instead of a new one.
        env.pop("GROK_SESSION_ID", None)
        env.pop("GROK_AGENT", None)
        env["TERM"] = "xterm-256color"
        env["COLORTERM"] = "truecolor"
        env["COLUMNS"] = str(cols)
        env["LINES"] = str(rows)
        proc = subprocess.Popen(
            cmd,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            cwd=cwd,
            env=env,
            start_new_session=True,
            close_fds=True,
        )
    except Exception:
        os.close(slave)
        os.close(master)
        raise
    os.close(slave)
    return proc, master


def resize_pty(master, proc, cols, rows):
    cols = dim_arg(str(cols), 80)
    rows = dim_arg(str(rows), 24)
    try:
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except OSError:
        return
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGWINCH)
        except OSError:
            pass


def write_master(master, data):
    view = data
    while view:
        n = os.write(master, view)
        if n <= 0:
            raise OSError("pty write failed")
        view = view[n:]


class Live:
    """One grok PTY. Sockets attach and leave. The process stays either way."""

    def __init__(self, sid, proc, master, cwd):
        self.sid = sid
        self.proc = proc
        self.master = master
        self.cwd = cwd
        self.started = time.time()
        self.buffer = bytearray()
        self.trimmed = False
        self.modes = {}
        self.clients = []
        self.lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.reader = None
        self.watcher = None
        self.closed = False
        self.sub_state = {"rows": {}, "order": []}
        self.sub_lock = threading.Lock()

    def alive(self):
        return self.proc is not None and self.proc.poll() is None and not self.closed

    def start(self):
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        self.watcher = threading.Thread(target=self._watch_updates, daemon=True)
        self.watcher.start()

    def _append(self, data):
        # strip_mouse_tracking holds a split ESC [ ? prefix, so a mode set
        # never straddles two chunks here.
        for params, final in DEC_MODE_RE.findall(data):
            for part in params.split(b";"):
                if part.isdigit() and int(part) in REPLAY_DEC_MODES:
                    self.modes[int(part)] = final
        self.buffer.extend(data)
        extra = len(self.buffer) - SCREEN_CAP
        if extra > 0:
            # A byte cut can land inside an escape sequence. xterm then prints
            # its tail as text (";20;20;20m"), and grok never redraws those
            # cells because it believes they already hold what it drew.
            start = self.buffer.find(FRAME_START, extra)
            if start < 0:
                start = self.buffer.find(b"\x1b", extra)
            if start < 0:
                start = extra
            del self.buffer[:start]
            self.trimmed = True

    def _replay(self):
        if not self.buffer:
            return b""
        if not self.trimmed:
            return bytes(self.buffer)
        head = b"".join(
            b"\x1b[?%d%s" % (mode, self.modes[mode])
            for mode in REPLAY_DEC_MODES
            if mode in self.modes
        )
        return head + bytes(self.buffer)

    def _enqueue(self, client, data, opcode=2):
        # Binary terminal bytes can split. A text frame must stay one message.
        if opcode != 2:
            client.queue.put((opcode, data))
            return
        for i in range(0, len(data), FRAME_CHUNK):
            client.queue.put((opcode, data[i:i + FRAME_CHUNK]))

    def subagent_rows(self):
        with self.sub_lock:
            rows = []
            for key in self.sub_state["order"]:
                row = self.sub_state["rows"].get(key)
                if not row:
                    continue
                rows.append({
                    "id": row["id"],
                    "description": row["description"],
                    "status": row["status"],
                    "title": row["title"],
                })
            return rows

    def _subagent_frame(self):
        payload = {"type": "subagents", "subagents": self.subagent_rows()}
        return json.dumps(payload).encode("utf-8")

    def _publish_subagents(self):
        frame = self._subagent_frame()
        with self.lock:
            clients = list(self.clients)
        for client in clients:
            if client.alive:
                client.queue.put((1, frame))

    def _publish_ended(self):
        frame = json.dumps({"type": "ended"}).encode("utf-8")
        with self.lock:
            clients = list(self.clients)
        for client in clients:
            if client.alive:
                client.queue.put((1, frame))

    def attach(self, handler):
        client = _Client(handler)
        sender = threading.Thread(target=self._send_loop, args=(client,), daemon=True)
        client.sender = sender
        sender.start()
        with self.lock:
            self.clients.append(client)
            snap = self._replay()
            if snap:
                self._enqueue(client, snap)
        client.queue.put((1, self._subagent_frame()))
        return client

    def detach(self, client):
        if client is None:
            return
        with self.lock:
            try:
                self.clients.remove(client)
            except ValueError:
                pass
        client.alive = False
        client.queue.put(None)

    def write(self, data):
        # A click after exit raises EIO, the handler closes the socket, and
        # the page starts another grok. Ignore that write. The ended frame
        # is what tells the browser the process is gone.
        if self.proc is None or self.proc.poll() is not None or self.closed:
            return
        with self.write_lock:
            if self.proc.poll() is not None or self.closed:
                return
            try:
                write_master(self.master, data)
            except OSError:
                if self.proc.poll() is not None or self.closed:
                    return
                raise

    def resize(self, cols, rows):
        resize_pty(self.master, self.proc, cols, rows)

    def repaint(self, cols, rows):
        # The replay is only an approximation of the screen: it can be trimmed,
        # and it was drawn for whatever size the PTY had then. grok redraws
        # only cells it changed and ignores a SIGWINCH at the same size, so
        # step one row away and back to make it clear and paint every cell.
        rows = dim_arg(str(rows), 24)
        bump = rows - 1 if rows > 1 else rows + 1
        resize_pty(self.master, self.proc, cols, bump)
        time.sleep(REPAINT_PAUSE)
        resize_pty(self.master, self.proc, cols, rows)

    def _read(self):
        hold = b""
        try:
            while not STOP.is_set() and not self.closed:
                ready, _, _ = select.select([self.master], [], [], 0.4)
                if not ready:
                    if self.proc.poll() is not None:
                        break
                    continue
                try:
                    data = os.read(self.master, 32768)
                except OSError:
                    if self.proc.poll() is not None or self.closed:
                        break
                    continue
                if not data:
                    if self.proc.poll() is not None:
                        break
                    continue
                data, hold = strip_mouse_tracking(data, hold)
                if not data:
                    continue
                with self.lock:
                    self._append(data)
                    clients = list(self.clients)
                    for client in clients:
                        self._enqueue(client, data)
        finally:
            # A client leaving does not get here. This thread only ends when
            # the PTY closes or the server is stopping. Closing a live master
            # would SIGHUP grok, so do that only once the process is gone.
            exited = self.proc.poll() is not None
            dead = exited or STOP.is_set()
            if dead:
                if exited:
                    self._publish_ended()
                self.closed = True
                with LIVE_LOCK:
                    if LIVE.get(self.sid) is self:
                        LIVE.pop(self.sid, None)
                try:
                    os.close(self.master)
                except OSError:
                    pass
                print("term end sid=%s poll=%s" % (self.sid, self.proc.poll()), flush=True)

    def _send_loop(self, client):
        while True:
            item = client.queue.get()
            if item is None or not client.alive:
                return
            opcode, payload = item
            try:
                client.handler._send_frame(opcode, payload)
            except (EOFError, OSError, RuntimeError, ValueError):
                client.alive = False
                return

    def _watch_updates(self):
        offsets = {}
        seen = set()
        while not STOP.is_set() and not self.closed:
            changed = self._pull_updates(offsets, seen)
            if changed:
                self._publish_subagents()
            if self.proc.poll() is not None:
                break
            time.sleep(0.25)
        if self._pull_updates(offsets, seen):
            self._publish_subagents()

    def _pull_updates(self, offsets, seen):
        paths = []
        parent = session_updates_path(self.sid)
        if parent:
            paths.append((parent, None))
        with self.sub_lock:
            children = []
            for row in self.sub_state["rows"].values():
                if row.get("status") == "running" and row.get("child"):
                    children.append(row["child"])
        for child in children:
            path = session_updates_path(child)
            if path:
                paths.append((path, child))
        changed = False
        for path, owner in paths:
            if self._consume_updates(path, owner, offsets):
                changed = True
        with self.sub_lock:
            current = set(self.sub_state["order"])
        for key in current - seen:
            row = None
            with self.sub_lock:
                raw = self.sub_state["rows"].get(key)
                if raw:
                    row = (raw["id"], raw.get("child"), raw.get("description"))
            if row:
                print(
                    "subagent sid=%s child=%s desc=%s" % (row[0], row[1], row[2]),
                    flush=True,
                )
        seen.update(current)
        return changed

    def _consume_updates(self, path, owner, offsets):
        try:
            size = os.path.getsize(path)
        except OSError:
            return False
        pos, pending = offsets.get(path, (0, b""))
        if size < pos:
            pos = 0
            pending = b""
        if size == pos:
            return False
        try:
            handle = open(path, "rb")
        except OSError:
            return False
        with handle:
            try:
                handle.seek(pos)
                data = handle.read(1024 * 1024)
            except OSError:
                return False
        if not data:
            return False
        pos += len(data)
        pending += data
        parts = pending.split(b"\n")
        pending = parts[-1]
        if len(pending) > 8 * 1024 * 1024:
            pending = b""
        offsets[path] = (pos, pending)
        changed = False
        for raw in parts[:-1]:
            raw = raw.strip()
            if not raw or b"sessionUpdate" not in raw:
                continue
            try:
                obj = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue
            params = obj.get("params")
            upd = params.get("update") if isinstance(params, dict) else None
            with self.sub_lock:
                if apply_session_update(self.sub_state, upd, owner):
                    changed = True
        return changed

    def shutdown(self):
        self.closed = True
        kill_proc(self.proc)
        try:
            os.close(self.master)
        except OSError:
            pass


class _Client:
    def __init__(self, handler):
        self.handler = handler
        self.queue = queue.Queue()
        self.alive = True


def open_live(sid, cmd, cwd, cols, rows):
    with LIVE_LOCK:
        if RESTART.is_set():
            raise RuntimeError("Ergosphere is restarting")
        live = LIVE.get(sid)
        if live is not None and live.alive():
            return live, True
        if live is not None:
            LIVE.pop(sid, None)
        proc, master = spawn_grok(cmd, cwd, cols, rows)
        live = Live(sid, proc, master, cwd)
        LIVE[sid] = live
        live.start()
    # Log after release: a stalled log write must not block other launches.
    print("term pid=%s sid=%s cmd=%s" % (proc.pid, sid, " ".join(cmd)), flush=True)
    return live, False


def list_live():
    with LIVE_LOCK:
        lives = list(LIVE.values())
    rows = []
    for live in lives:
        rows.append({
            "id": live.sid,
            "pid": live.proc.pid,
            "cwd": live.cwd,
            "alive": live.proc.poll() is None,
            "started": live.started,
        })
    rows.sort(key=lambda row: row["started"], reverse=True)
    return rows


_TURN_MARK = b"agent turn in progress"


def _proc_ppid_map():
    ppids = {}
    try:
        names = os.listdir("/proc")
    except OSError:
        return ppids
    for name in names:
        if not name.isdigit():
            continue
        ppid = proc_stat_ppid(name)
        if ppid is None:
            continue
        ppids[int(name)] = ppid
    return ppids


def _turn_in_children(pid, ppids):
    # Direct or deeper. The mark is on systemd-inhibit, not on grok itself.
    children = {}
    for child, parent in ppids.items():
        children.setdefault(parent, []).append(child)
    stack = list(children.get(pid, []))
    seen = set()
    while stack:
        child = stack.pop()
        if child in seen or child == pid:
            continue
        seen.add(child)
        raw = read_proc_cmdline(child)
        if raw and _TURN_MARK in raw.replace(b"\0", b" "):
            return True
        stack.extend(children.get(child, []))
    return False


def _cmdline_session(raw):
    # First argv token that is a session id on disk. That grok owns no other.
    for part in raw.split(b"\0"):
        if not part:
            continue
        try:
            token = part.decode("ascii")
        except UnicodeDecodeError:
            continue
        if not SID_TOKEN_RE.match(token) or not SID_RE.match(token):
            continue
        if session_dir_for(token):
            return token
    return None


def _fd_session(pid):
    # Lowest fd number, not lexical order. Other session files do not count.
    root = os.path.abspath(str(sessions_root()))
    prefix = root + os.sep
    fd_dir = "/proc/%d/fd" % pid
    try:
        names = os.listdir(fd_dir)
    except OSError:
        return None
    nums = []
    for name in names:
        if name.isdigit():
            nums.append(int(name))
    nums.sort()
    for num in nums:
        try:
            target = os.readlink(os.path.join(fd_dir, str(num)))
        except OSError:
            continue
        if not target.endswith("/events.jsonl") or not target.startswith(prefix):
            continue
        sid = os.path.basename(os.path.dirname(target))
        if not SID_TOKEN_RE.match(sid) or not SID_RE.match(sid):
            continue
        if not session_dir_for(sid):
            continue
        return sid
    return None


def dashboard_owners():
    """Session id -> {pid, state} for a live grok that owns it.

    Command-line session id wins and blocks every other file that grok has
    open. With no such argument, the session is the one in the lowest
    events.jsonl fd under the sessions directory. Working means a child
    command contains "agent turn in progress". Idle means the grok is alive
    and no child has that mark. Does not start or signal a process, and does
    not read environ.
    """
    try:
        pid_names = os.listdir("/proc")
    except OSError:
        pid_names = []
    groks = []
    for pid_text in pid_names:
        if not pid_text.isdigit():
            continue
        pid = int(pid_text)
        if pid == os.getpid():
            continue
        raw = read_proc_cmdline(pid)
        if not raw or not grok_cmdline(raw):
            continue
        groks.append((pid, raw))
    groks.sort()
    owned = {}
    need_fd = []
    for pid, raw in groks:
        sid = _cmdline_session(raw)
        if sid:
            if sid not in owned:
                owned[sid] = pid
            continue
        need_fd.append(pid)
    for pid in need_fd:
        sid = _fd_session(pid)
        if not sid or sid in owned:
            continue
        owned[sid] = pid
    ppids = _proc_ppid_map()
    result = {}
    for sid, pid in owned.items():
        raw = read_proc_cmdline(pid)
        if not raw or not grok_cmdline(raw):
            continue
        state = "working" if _turn_in_children(pid, ppids) else "idle"
        result[sid] = {"pid": pid, "state": state}
    return result


# summary.json updated_at is ISO-8601, for example
# 2026-11-02T16:42:52.714510437Z. The fraction is optional. The zone is Z or a numeric offset.
_UPDATED_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?([Zz]|[+-]\d{2}:\d{2}(?::\d{2})?)$"
)


def dashboard_stamp(value):
    """Local yyyy-mm-dd hh:mm:ss and unix seconds, or blanks.

    Only the ISO-8601 shape above converts. A missing or other value stays
    blank so the page does not invent a clock time.
    """
    if not isinstance(value, str):
        return "", ""
    match = _UPDATED_RE.match(value.strip())
    if not match:
        return "", ""
    year, month, day, hour, minute, second, frac, zone = match.groups()
    try:
        micro = int(((frac or "") + "000000")[:6])
        if zone in ("Z", "z"):
            tz = timezone.utc
        else:
            sign = 1 if zone[0] == "+" else -1
            parts = zone[1:].split(":")
            offset = timedelta(
                hours=int(parts[0]),
                minutes=int(parts[1]),
                seconds=int(parts[2]) if len(parts) > 2 else 0,
            )
            tz = timezone(sign * offset)
        moment = datetime(
            int(year), int(month), int(day),
            int(hour), int(minute), int(second), micro,
            tzinfo=tz,
        )
    except (ValueError, OverflowError):
        return "", ""
    local = moment.astimezone()
    return local.strftime("%Y-%m-%d %H:%M:%S"), str(int(moment.timestamp()))


def dashboard_sort_key(row):
    _shown, stamp = dashboard_stamp(row.get("updated") or "")
    if not stamp:
        return (1, 0, row.get("id") or "")
    return (0, -int(stamp), row.get("id") or "")


def dashboard_head(section):
    cols = (
        ("updated", "last update"),
        ("title", "session title"),
        ("sid", "session id"),
        ("cwd", "working directory"),
    )
    buttons = []
    for key, label in cols:
        text = label + " ↓" if key == "updated" else label
        buttons.append(
            '<button type="button" data-col="%s" data-label="%s">%s</button>'
            % (key, label, text)
        )
    return '<div class="head" data-section="%s">%s</div>' % (section, "".join(buttons))


def dashboard_session_row(section, seq, row, pid, hidden=False):
    title = row.get("title") if isinstance(row.get("title"), str) else ""
    sid = row.get("id") if isinstance(row.get("id"), str) else ""
    cwd = row.get("cwd") if isinstance(row.get("cwd"), str) else ""
    shown, stamp = dashboard_stamp(row.get("updated") or "")
    href = "/chat?resume=" + sid
    esc_href = html.escape(href, quote=True)
    pid_attr = ""
    if pid is not None:
        pid_attr = ' data-pid="%d"' % int(pid)
    hide_attr = " hidden" if hidden else ""
    inner = (
        '<span class="c-updated">%s</span><span class="c-title">%s</span>'
        '<span class="c-sid">%s</span><span class="c-cwd">%s</span>'
    ) % (
        html.escape(shown),
        html.escape(title or "(no title)"),
        html.escape(sid),
        html.escape(cwd),
    )
    return (
        '<a class="row session" href="%s" data-open="%s" data-kind="session" '
        'data-section="%s" data-seq="%d" data-updated="%s" data-title="%s" '
        'data-sid="%s" data-cwd="%s"%s%s>%s</a>'
    ) % (
        esc_href,
        esc_href,
        section,
        seq,
        html.escape(stamp, quote=True),
        html.escape(title, quote=True),
        html.escape(sid, quote=True),
        html.escape(cwd, quote=True),
        pid_attr,
        hide_attr,
        inner,
    )


def script_json(value):
    # JSON text inside a script element. Escape so a path cannot close the tag.
    text = json.dumps(value, ensure_ascii=True, separators=(",", ":"))
    return (
        text.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )


def dashboard_cwd_choices(cwd_defaults):
    # Directory choices for the form. Not agents, and not written into
    # ergosphere.json.
    ordered = []
    if isinstance(cwd_defaults, dict):
        ordered.extend(cwd_defaults.values())
    choices = []
    seen = set()
    for path in ordered:
        if not isinstance(path, str) or not path or path in seen:
            continue
        seen.add(path)
        choices.append(path)
    return choices


def dashboard_page(sessions, agents, cwd_defaults):
    # summary.json has updated_at, not Working or Idle. Those come from live
    # grok processes. A session with no live owner stays under Older sessions.
    # Every older summary is in the page. The script shows 20, then 50 more.
    owners = dashboard_owners()
    by_id = {}
    for row in sessions:
        by_id[row["id"]] = row

    def owned_row(sid):
        row = by_id.get(sid)
        if row:
            return row
        extra = find_session(sid)
        if extra:
            return extra
        return {"id": sid, "title": "", "cwd": "", "updated": ""}

    work = []
    idle = []
    for sid in owners:
        info = owners[sid]
        row = owned_row(sid)
        if info["state"] == "working":
            work.append((row, info["pid"]))
        else:
            idle.append((row, info["pid"]))
    work.sort(key=lambda item: dashboard_sort_key(item[0]))
    idle.sort(key=lambda item: dashboard_sort_key(item[0]))
    older = [row for row in sessions if row["id"] not in owners]
    older.sort(key=dashboard_sort_key)
    blocks = [
        '<a class="row selected" href="#new-agent" data-kind="new">+ New Agent</a>',
        '<div class="bar"></div>',
        '<h2 class="group" id="count-working">Active Sessions: %d</h2>' % len(work),
        dashboard_head("working"),
        '<div class="sec" id="sec-working">',
        '<div class="empty"%s>- none -</div>' % ("" if not work else " hidden"),
    ]
    for seq, (row, pid) in enumerate(work):
        blocks.append(dashboard_session_row("working", seq, row, pid))
    blocks.append("</div>")
    blocks.append('<div class="bar"></div>')
    blocks.append('<h2 class="group" id="count-idle">Idle Sessions: %d</h2>' % len(idle))
    blocks.append(dashboard_head("idle"))
    blocks.append('<div class="sec" id="sec-idle">')
    blocks.append('<div class="empty"%s>- none -</div>' % ("" if not idle else " hidden"))
    for seq, (row, pid) in enumerate(idle):
        blocks.append(dashboard_session_row("idle", seq, row, pid))
    blocks.append("</div>")
    blocks.append('<div class="bar"></div>')
    blocks.append('<h2 class="group">Older Sessions</h2>')
    blocks.append(dashboard_head("older"))
    blocks.append('<div class="sec" id="sec-older">')
    blocks.append('<div class="empty"%s>- none -</div>' % ("" if not older else " hidden"))
    for seq, row in enumerate(older):
        blocks.append(dashboard_session_row(
            "older", seq, row, None, seq >= OLDER_WINDOW
        ))
    if len(older) > OLDER_WINDOW:
        blocks.append(
            '<a class="more" id="load-more" href="#load-more" data-kind="more">[load more]</a>'
        )
    blocks.append("</div>")
    body = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Ergosphere</title>
<style>
body { background: #000; color: #fff; font: 14px/1.45 ui-monospace, monospace; margin: 0; padding: 0.6rem 0.8rem 3rem; }
a, a:visited, a:hover, a:active { color: #fff; }
h2.group { font-size: 14px; font-weight: normal; color: #fff; margin: 0.4rem 0; }
input, button, select { font: inherit; color: #fff; background: #000; border: 1px solid #fff; }
input:focus, button:focus, select:focus, a:focus { outline: 1px solid #fff; }
#find-bar { display: flex; align-items: center; width: 36rem; max-width: 100%; margin: 0 0 0.6rem; box-sizing: border-box; background: #000; color: #fff; border: 1px solid #fff; font: inherit; }
#find-bar .find-mark { flex: 0 0 auto; display: flex; align-items: center; padding: 0 0.45rem; background: #000; color: #fff; line-height: 0; }
#find { display: block; flex: 1 1 auto; width: auto; min-width: 0; margin: 0; border: 0; background: #000; color: #fff; font: inherit; }
#find:focus { outline: none; }
#find-bar:focus-within { outline: 1px solid #fff; }
.bar { height: 1px; background: #fff; width: 100%; margin: 0.35rem 0; }
.head, a.row.session { display: grid; grid-template-columns: 19ch minmax(0, 1.3fr) minmax(0, 1.6fr) minmax(0, 2fr); column-gap: 0.8rem; align-items: baseline; }
.head { margin: 0.15rem 0; }
.head button { border: 0; text-align: left; padding: 0.12rem 0.35rem; cursor: pointer; }
a.row { display: block; padding: 0.12rem 0.35rem; color: #fff; text-decoration: none; scroll-margin-bottom: 2.6rem; }
a.row[data-kind="new"], a.more { display: inline-block; width: auto; }
a.more { color: #fff; text-decoration: none; padding: 0.12rem 0.35rem; }
a.more.selected, a.more.selected:visited, a.more.selected:hover { background: #fff; color: #000; }
a.more[hidden] { display: none; }
a.row.session span { overflow-wrap: anywhere; }
a.row[hidden] { display: none; }
a.row.selected, a.row.selected:visited, a.row.selected:hover { background: #fff; color: #000; }
a.row.selected span { color: #000; }
.empty { color: #fff; padding: 0.12rem 0.35rem; text-align: center; }
#corner { position: absolute; top: 0.6rem; right: 0.8rem; font-size: 11px; line-height: 1.45; color: #fff; }
footer { position: fixed; left: 0; right: 0; bottom: 0; width: 100%; box-sizing: border-box; background: #000; color: #fff; padding: 0.4rem 0.8rem; z-index: 4; }
#new-agent { position: fixed; z-index: 3; left: 50%; top: 18%; transform: translateX(-50%); width: 36rem; max-width: calc(100% - 2rem); box-sizing: border-box; background: #000; color: #fff; border: 1px solid #fff; padding: 0.8rem 1rem; }
#new-agent[hidden] { display: none; }
#new-agent label { display: block; margin: 0.45rem 0; }
#new-agent input, #new-agent select { display: block; width: 100%; box-sizing: border-box; margin-top: 0.15rem; }
#new-agent input:focus, #new-agent select:focus, #new-agent button:focus { background: #fff; color: #000; }
#new-agent button { margin-top: 0.6rem; }
#new-agent .pair { display: flex; gap: 0.8rem; margin: 0.45rem 0; }
#new-agent .pair label { display: flex; align-items: center; gap: 0.45rem; flex: 1 1 0; min-width: 0; margin: 0; }
#new-agent .pair select { display: block; width: auto; flex: 1 1 auto; min-width: 0; margin-top: 0; }
#new-agent .line { display: flex; align-items: center; flex-wrap: nowrap; gap: 0.75rem; margin: 0.45rem 0; }
#new-agent .line label { display: flex; align-items: center; gap: 0.35rem; margin: 0; white-space: nowrap; }
#new-agent .line input[type="radio"] { appearance: none; -webkit-appearance: none; display: inline-block; width: 0.95rem; height: 0.95rem; min-width: 0.95rem; margin: 0; padding: 0; flex: 0 0 auto; border: 1px solid #fff; border-radius: 50%; background: #000; color: #fff; box-sizing: border-box; }
#new-agent .line input[type="radio"]:focus { outline: 1px solid #fff; background: #000; color: #fff; }
#new-agent .line input[type="radio"]:checked, #new-agent .line input[type="radio"]:checked:focus { background: radial-gradient(circle, #000 0 28%, #fff 32% 100%); color: #fff; }
#new-agent .na-error[hidden] { display: none; }
#new-agent .combo { position: relative; display: block; }
#new-agent .combo-list { position: absolute; z-index: 6; left: 0; right: 0; top: 100%; margin: 0; padding: 0; max-height: 14rem; overflow: auto; background: #000; color: #fff; border: 1px solid #fff; font: inherit; }
#new-agent .combo-list[hidden] { display: none; }
#new-agent .combo-list .opt { font: inherit; color: #fff; background: #000; padding: 0 2px; line-height: 1.45; margin: 0; cursor: pointer; overflow-wrap: anywhere; }
#new-agent .combo-list .opt.hi { background: #fff; color: #000; }
</style>
</head>
<body>
<div id="corner"><a id="settings-link" href="/settings">Settings</a> | <span id="version">v@@VERSION@@</span></div>
<div id="find-bar"><span class="find-mark" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="#fff" stroke-width="1.6" aria-hidden="true" focusable="false"><circle cx="6.5" cy="6.5" r="4.25" fill="none"/><path d="M9.8 9.8 L13.2 13.2" fill="none" stroke="#fff" stroke-linecap="round"/></svg></span><input id="find" type="search" autocomplete="off" aria-label="Filter sessions"></div>
<div class="rows" id="rows">
@@ROWS@@
</div>
<div id="new-agent" hidden>
<form id="new-form" action="#new-agent">
<label>Agent <span class="combo"><input id="na-agent" name="agent" autocomplete="off"><div class="combo-list" id="na-agent-list" hidden></div></span></label>
<label>Working directory <span class="combo"><input id="na-cwd" name="cwd" autocomplete="off"><div class="combo-list" id="na-cwd-list" hidden></div></span></label>
<div class="pair">
<label>Model <select id="na-model" name="model"><option value="" selected></option></select></label>
<label>Effort <select id="na-effort" name="effort"><option value="low">low</option><option value="medium" selected>medium</option><option value="high">high</option><option value="xhigh">xhigh</option></select></label>
</div>
<div id="na-error" class="na-error" hidden></div>
<div class="line">Approval
<label><input type="radio" name="approval" value="ask" checked> ask</label>
<label><input type="radio" name="approval" value="auto"> auto</label>
<label><input type="radio" name="approval" value="always-approve"> always approve</label>
</div>
<button type="submit">Create</button>
</form>
</div>
<footer>↑/↓ select | Enter: open | Ctrl+Enter: new tab | Alt+N new agent</footer>
<script>
var cwdDefaults = @@CWD@@;
var agentNames = @@AGENTS@@;
var cwdChoices = @@CWDS@@;
var rows = [];
var selectedEl = null;
var listState = {
  "na-agent": {q: "", hi: -1, items: []},
  "na-cwd": {q: "", hi: -1, items: []}
};
if (!agentNames || typeof agentNames.length !== "number") agentNames = [];
if (!cwdChoices || typeof cwdChoices.length !== "number") cwdChoices = [];
var defaultEfforts = ["low", "medium", "high", "xhigh"];
var modelRows = [];
var modelFetch = 0;
var launchBusy = false;
var sortState = {
  working: {col: "updated", dir: "desc"},
  idle: {col: "updated", dir: "desc"},
  older: {col: "updated", dir: "desc"}
};
var olderLimit = @@OLDER_WINDOW@@;
var olderStep = @@OLDER_STEP@@;
function rowClass(el, on) {
  var base = el.getAttribute("data-kind") === "session" ? "row session" : "row";
  return on ? base + " selected" : base;
}
function visibleRows() {
  var all = document.querySelectorAll("a.row, a.more");
  var out = [];
  var i;
  for (i = 0; i < all.length; i++) {
    if (!all[i].hidden) out.push(all[i]);
  }
  return out;
}
function paint() {
  var all = document.querySelectorAll("a.row, a.more");
  var i;
  for (i = 0; i < all.length; i++) {
    if (all[i].getAttribute("data-kind") === "more") {
      all[i].className = all[i] === selectedEl ? "more selected" : "more";
    } else {
      all[i].className = rowClass(all[i], all[i] === selectedEl);
    }
  }
  if (selectedEl && selectedEl.scrollIntoView) {
    selectedEl.scrollIntoView({block: "nearest"});
  }
}
function sync() {
  rows = visibleRows();
  var i, found = false;
  for (i = 0; i < rows.length; i++) {
    if (rows[i] === selectedEl) found = true;
  }
  if (!found) selectedEl = rows.length ? rows[0] : null;
  paint();
}
function dialogOpen() {
  var el = document.getElementById("new-agent");
  return el && !el.hidden;
}
function comboSource(inputId) {
  if (inputId === "na-agent") return agentNames;
  return cwdChoices;
}
function renderList(inputId) {
  var st = listState[inputId];
  var src = comboSource(inputId);
  var q = (st.q || "").toLowerCase();
  var items = [];
  var i;
  var text;
  var box = document.getElementById(inputId + "-list");
  if (!q) {
    st.items = [];
    st.hi = -1;
    if (box) {
      box.textContent = "";
      box.hidden = true;
    }
    return;
  }
  for (i = 0; i < src.length; i++) {
    text = src[i];
    if (typeof text !== "string") continue;
    if (text.toLowerCase().indexOf(q) !== -1) items.push(text);
  }
  st.items = items;
  if (st.hi >= items.length) st.hi = items.length ? items.length - 1 : -1;
  if (!box) return;
  box.textContent = "";
  if (!items.length) {
    box.hidden = true;
    return;
  }
  for (i = 0; i < items.length; i++) {
    var opt = document.createElement("div");
    opt.className = i === st.hi ? "opt hi" : "opt";
    opt.setAttribute("data-value", items[i]);
    opt.textContent = items[i];
    box.appendChild(opt);
  }
  box.hidden = false;
  if (st.hi >= 0 && box.children[st.hi] && box.children[st.hi].scrollIntoView) {
    box.children[st.hi].scrollIntoView({block: "nearest"});
  }
}
function hideList(inputId) {
  var box = document.getElementById(inputId + "-list");
  if (box) box.hidden = true;
}
function showList(inputId) {
  listState[inputId].q = document.getElementById(inputId).value;
  listState[inputId].hi = -1;
  renderList(inputId);
}
function applyChoice(inputId, value) {
  document.getElementById(inputId).value = value;
  if (inputId === "na-agent") fillNewCwd();
}
function moveList(inputId, dir) {
  var st = listState[inputId];
  var box = document.getElementById(inputId + "-list");
  if (!box || box.hidden || !st.items.length) return false;
  var next = st.hi;
  var n = st.items.length;
  if (next < 0) next = dir > 0 ? 0 : n - 1;
  else next += dir;
  if (next < 0) next = 0;
  if (next >= n) next = n - 1;
  st.hi = next;
  applyChoice(inputId, st.items[next]);
  renderList(inputId);
  return true;
}
function onComboInput(inputId) {
  listState[inputId].q = document.getElementById(inputId).value;
  listState[inputId].hi = -1;
  renderList(inputId);
  if (inputId === "na-agent") fillNewCwd();
}
function pickOption(opt) {
  var box = opt.parentNode;
  if (!box || !box.id || box.id.slice(-5) !== "-list") return;
  var inputId = box.id.slice(0, -5);
  if (!listState[inputId]) return;
  var value = opt.getAttribute("data-value");
  if (value === null) return;
  applyChoice(inputId, value);
  var st = listState[inputId];
  var i;
  st.hi = -1;
  for (i = 0; i < st.items.length; i++) {
    if (st.items[i] === value) st.hi = i;
  }
  renderList(inputId);
}
function setModelError(text) {
  var el = document.getElementById("na-error");
  if (!el) return;
  if (text) {
    el.hidden = false;
    el.textContent = text;
  } else {
    el.hidden = true;
    el.textContent = "";
  }
}
function fillSelect(id, values, keep, prefer) {
  var sel = document.getElementById(id);
  var current = keep ? sel.value : "";
  var i, opt, found = false, first = "", hasPrefer = false;
  sel.textContent = "";
  if (!prefer) {
    opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "";
    sel.appendChild(opt);
  }
  for (i = 0; i < values.length; i++) {
    if (typeof values[i] !== "string" || !values[i]) continue;
    if (!first) first = values[i];
    if (prefer && values[i] === prefer) hasPrefer = true;
    opt = document.createElement("option");
    opt.value = values[i];
    opt.textContent = values[i];
    if (current && values[i] === current) found = true;
    sel.appendChild(opt);
  }
  if (found) sel.value = current;
  else if (prefer && hasPrefer) sel.value = prefer;
  else if (prefer) sel.value = first;
  else sel.value = "";
}
function effortsFor(modelId) {
  var i, row, levels;
  for (i = 0; i < modelRows.length; i++) {
    row = modelRows[i];
    if (!row || row.id !== modelId) continue;
    levels = row.efforts;
    if (levels && typeof levels.length === "number" && levels.length) return levels;
  }
  return defaultEfforts;
}
function applyModelList(data) {
  var rows = [];
  var i, row, levels, j, level;
  modelRows = [];
  if (data && data.models && typeof data.models.length === "number") {
    for (i = 0; i < data.models.length; i++) {
      row = data.models[i];
      if (!row || typeof row.id !== "string" || !row.id) continue;
      levels = [];
      if (row.efforts && typeof row.efforts.length === "number") {
        for (j = 0; j < row.efforts.length; j++) {
          level = row.efforts[j];
          if (typeof level === "string" && level) levels.push(level);
        }
      }
      rows.push(row.id);
      modelRows.push({id: row.id, efforts: levels});
    }
  }
  if (data && data.efforts && typeof data.efforts.length === "number" && data.efforts.length) {
    defaultEfforts = [];
    for (i = 0; i < data.efforts.length; i++) {
      if (typeof data.efforts[i] === "string" && data.efforts[i]) defaultEfforts.push(data.efforts[i]);
    }
    if (!defaultEfforts.length) defaultEfforts = ["low", "medium", "high", "xhigh"];
  }
  // Preselect the last launch's model and effort while they are still offered.
  var last = (data && data.last) || {};
  var lastModel = typeof last.model === "string" ? last.model : "";
  var lastEffort = typeof last.effort === "string" ? last.effort : "";
  fillSelect("na-model", rows, false);
  if (lastModel && rows.indexOf(lastModel) !== -1) document.getElementById("na-model").value = lastModel;
  var levels = effortsFor(document.getElementById("na-model").value);
  fillSelect("na-effort", levels, false, lastEffort && levels.indexOf(lastEffort) !== -1 ? lastEffort : "medium");
  if (data && data.agents && typeof data.agents.length === "number") {
    agentNames = [];
    for (i = 0; i < data.agents.length; i++) {
      if (typeof data.agents[i] === "string") agentNames.push(data.agents[i]);
    }
    var box = document.getElementById("na-agent-list");
    if (box && !box.hidden) renderList("na-agent");
  }
  if (data && data.ok === false) {
    setModelError((typeof data.error === "string" && data.error) ? data.error : "model list failed");
  } else {
    setModelError("");
  }
}
function loadModels() {
  modelFetch += 1;
  var token = modelFetch;
  setModelError("");
  fillSelect("na-model", [], false);
  fillSelect("na-effort", defaultEfforts, false, "medium");
  fetch("/models", {cache: "no-store"}).then(function (resp) {
    if (!resp.ok) throw new Error("model list failed");
    return resp.json();
  }).then(function (data) {
    if (token !== modelFetch || !dialogOpen()) return;
    applyModelList(data);
  }).catch(function () {
    if (token !== modelFetch || !dialogOpen()) return;
    modelRows = [];
    fillSelect("na-model", [], false);
    fillSelect("na-effort", defaultEfforts, true, "medium");
    setModelError("model list failed");
  });
}
function openNew() {
  document.getElementById("na-agent").value = "";
  document.getElementById("na-cwd").value = "";
  setApproval("ask");
  listState["na-agent"].q = "";
  listState["na-agent"].hi = -1;
  listState["na-cwd"].q = "";
  listState["na-cwd"].hi = -1;
  hideList("na-agent");
  hideList("na-cwd");
  document.getElementById("new-agent").hidden = false;
  document.getElementById("na-agent").focus();
  loadModels();
}
function closeNew() {
  hideList("na-agent");
  hideList("na-cwd");
  document.getElementById("new-agent").hidden = true;
}
function fillNewCwd() {
  var agent = document.getElementById("na-agent").value;
  if (Object.prototype.hasOwnProperty.call(cwdDefaults, agent)) {
    document.getElementById("na-cwd").value = cwdDefaults[agent];
  }
}
function approvalValue() {
  var nodes = document.querySelectorAll('#new-form input[name="approval"]');
  var i;
  for (i = 0; i < nodes.length; i++) {
    if (nodes[i].checked) return nodes[i].value;
  }
  return "ask";
}
function setApproval(value) {
  var nodes = document.querySelectorAll('#new-form input[name="approval"]');
  var i, matched = false;
  for (i = 0; i < nodes.length; i++) {
    nodes[i].checked = nodes[i].value === value;
    if (nodes[i].checked) matched = true;
  }
  if (!matched && nodes.length) nodes[0].checked = true;
}
function openBlank(href) {
  var a = document.createElement("a");
  a.href = href;
  a.target = "_blank";
  a.rel = "noopener";
  document.body.appendChild(a);
  a.click();
  a.remove();
}
function launchNew(blank) {
  if (launchBusy) return;
  var agent = document.getElementById("na-agent").value.trim();
  var cwd = document.getElementById("na-cwd").value.trim();
  if (!agent && !cwd) {
    setModelError("Choose a working directory.");
    return;
  }
  var payload = {
    agent: agent,
    cwd: cwd,
    approval: approvalValue(),
    model: document.getElementById("na-model").value,
    effort: document.getElementById("na-effort").value
  };
  launchBusy = true;
  setModelError("");
  fetch("/launch", {
    method: "POST",
    cache: "no-store",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(payload)
  }).then(function (resp) {
    return resp.json().then(function (data) {
      return {ok: resp.ok, data: data};
    }, function () {
      return {ok: false, data: null};
    });
  }).then(function (res) {
    launchBusy = false;
    var data = res.data || {};
    if (!res.ok || typeof data.id !== "string" || !data.id) {
      setModelError(typeof data.error === "string" && data.error ? data.error : "could not start");
      return;
    }
    var href = "/chat?resume=" + encodeURIComponent(data.id);
    if (blank) {
      openBlank(href);
      return;
    }
    location.href = href;
  }).catch(function () {
    launchBusy = false;
    setModelError("could not start");
  });
}
function openSelected(blank) {
  if (!selectedEl) return;
  if (selectedEl.getAttribute("data-kind") === "more") {
    revealOlder();
    return;
  }
  if (selectedEl.getAttribute("data-kind") === "new") {
    openNew();
    return;
  }
  var href = selectedEl.getAttribute("data-open");
  if (!href) return;
  if (blank) {
    openBlank(href);
    return;
  }
  location.href = href;
}
function paintSort(section) {
  var st = sortState[section];
  if (!st) return;
  var head = document.querySelector('.head[data-section="' + section + '"]');
  if (!head) return;
  var buttons = head.querySelectorAll("button");
  var i, col, label;
  for (i = 0; i < buttons.length; i++) {
    col = buttons[i].getAttribute("data-col");
    label = buttons[i].getAttribute("data-label") || "";
    if (col === st.col) {
      buttons[i].textContent = label + (st.dir === "asc" ? " ↑" : " ↓");
    } else {
      buttons[i].textContent = label;
    }
  }
}
function sortSection(section, col) {
  var st = sortState[section];
  if (!st) return;
  if (st.col === col) st.dir = st.dir === "asc" ? "desc" : "asc";
  else {
    st.col = col;
    st.dir = col === "updated" ? "desc" : "asc";
  }
  var box = document.getElementById("sec-" + section);
  if (!box) return;
  var items = Array.prototype.slice.call(box.querySelectorAll("a.row"));
  items.sort(function (a, b) {
    var av = a.getAttribute("data-" + col) || "";
    var bv = b.getAttribute("data-" + col) || "";
    var c = 0;
    if (col === "updated") {
      if (!av && !bv) c = 0;
      else if (!av) return 1;
      else if (!bv) return -1;
      else {
        var an = parseInt(av, 10);
        var bn = parseInt(bv, 10);
        c = an === bn ? 0 : (an < bn ? -1 : 1);
        if (st.dir === "desc") c = -c;
      }
    } else {
      av = av.toLowerCase();
      bv = bv.toLowerCase();
      if (av < bv) c = -1;
      else if (av > bv) c = 1;
      if (st.dir === "desc") c = -c;
    }
    if (c) return c;
    return parseInt(a.getAttribute("data-seq") || "0", 10) - parseInt(b.getAttribute("data-seq") || "0", 10);
  });
  var i;
  for (i = 0; i < items.length; i++) box.appendChild(items[i]);
  if (section === "older") paintOlder();
  paintSort(section);
  sync();
}
function olderBlob(node) {
  return ((node.getAttribute("data-title") || "") + "\\n" +
    (node.getAttribute("data-sid") || "") + "\\n" +
    (node.getAttribute("data-cwd") || "")).toLowerCase();
}
function paintOlder() {
  var q = document.getElementById("find").value.toLowerCase();
  var box = document.getElementById("sec-older");
  if (!box) return;
  var nodes = box.querySelectorAll("a.row.session");
  var more = document.getElementById("load-more");
  var i, node, last, remain;
  if (q) {
    for (i = 0; i < nodes.length; i++) {
      nodes[i].hidden = olderBlob(nodes[i]).indexOf(q) === -1;
    }
    if (more) more.hidden = true;
    return;
  }
  remain = false;
  last = null;
  for (i = 0; i < nodes.length; i++) {
    node = nodes[i];
    if (i < olderLimit) {
      node.hidden = false;
      last = node;
    } else {
      node.hidden = true;
      remain = true;
    }
  }
  if (!more) return;
  if (!remain) {
    more.remove();
    return;
  }
  more.hidden = false;
  if (last && last.nextSibling) box.insertBefore(more, last.nextSibling);
  else box.appendChild(more);
}
function revealOlder() {
  olderLimit += olderStep;
  paintOlder();
  paintCounts();
  sync();
}
function paintCounts() {
  var specs = [["working", "Active Sessions"], ["idle", "Idle Sessions"]];
  var s, i, nodes, n, box, empty, el;
  for (s = 0; s < specs.length; s++) {
    box = document.getElementById("sec-" + specs[s][0]);
    nodes = box ? box.querySelectorAll("a.row.session") : [];
    n = 0;
    for (i = 0; i < nodes.length; i++) if (!nodes[i].hidden) n++;
    el = document.getElementById("count-" + specs[s][0]);
    if (el) el.textContent = specs[s][1] + ": " + n;
    empty = box ? box.querySelector(".empty") : null;
    if (empty) empty.hidden = n !== 0;
  }
  box = document.getElementById("sec-older");
  if (!box) return;
  nodes = box.querySelectorAll("a.row.session");
  n = 0;
  for (i = 0; i < nodes.length; i++) if (!nodes[i].hidden) n++;
  empty = box.querySelector(".empty");
  if (empty) empty.hidden = n !== 0;
}
function applyFilter() {
  var q = document.getElementById("find").value.toLowerCase();
  var nodes = document.querySelectorAll("#sec-working a.row.session, #sec-idle a.row.session");
  var i;
  for (i = 0; i < nodes.length; i++) {
    nodes[i].hidden = q !== "" && olderBlob(nodes[i]).indexOf(q) === -1;
  }
  paintOlder();
  paintCounts();
  sync();
}
document.addEventListener("keydown", function (ev) {
  var idx = -1;
  var i;
  if (ev.altKey && !ev.ctrlKey && !ev.metaKey && !ev.shiftKey &&
      (ev.key === "n" || ev.key === "N" || ev.code === "KeyN")) {
    ev.preventDefault();
    if (!dialogOpen()) openNew();
    return;
  }
  if (dialogOpen()) {
    var comboId = "";
    var active = document.activeElement;
    if (active && (active.id === "na-agent" || active.id === "na-cwd")) comboId = active.id;
    if (ev.key === "Escape") {
      ev.preventDefault();
      closeNew();
    } else if ((ev.key === "ArrowDown" || ev.key === "ArrowUp") && comboId) {
      ev.preventDefault();
      moveList(comboId, ev.key === "ArrowDown" ? 1 : -1);
    } else if (ev.key === "Enter") {
      ev.preventDefault();
      if (ev.shiftKey) return;
      if (ev.ctrlKey && (ev.altKey || ev.metaKey)) return;
      if (active && (active.id === "na-model" || active.id === "na-effort")) return;
      launchNew(ev.ctrlKey);
    }
    return;
  }
  if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
    ev.preventDefault();
    rows = visibleRows();
    for (i = 0; i < rows.length; i++) {
      if (rows[i] === selectedEl) idx = i;
    }
    if (ev.key === "ArrowDown" && idx < rows.length - 1) selectedEl = rows[idx + 1];
    if (ev.key === "ArrowUp" && idx > 0) selectedEl = rows[idx - 1];
    paint();
    return;
  }
  if (ev.key === "Enter") {
    if (document.activeElement && document.activeElement.id === "settings-link") return;
    ev.preventDefault();
    if (ev.shiftKey) return;
    if (ev.ctrlKey && (ev.altKey || ev.metaKey)) return;
    openSelected(ev.ctrlKey);
  }
});
document.addEventListener("click", function (ev) {
  var node = ev.target;
  while (node && node !== document) {
    if (node.getAttribute && node.getAttribute("data-kind") === "more") {
      ev.preventDefault();
      revealOlder();
      return;
    }
    if (node.getAttribute && node.getAttribute("data-kind") === "new") {
      ev.preventDefault();
      openNew();
      return;
    }
    node = node.parentNode;
  }
  var btn = ev.target;
  if (!btn || !btn.getAttribute) return;
  var col = btn.getAttribute("data-col");
  if (!col || !btn.parentNode || !btn.parentNode.getAttribute) return;
  var section = btn.parentNode.getAttribute("data-section");
  if (!section) return;
  sortSection(section, col);
});
document.getElementById("find").addEventListener("input", applyFilter);
function bindCombo(inputId) {
  var input = document.getElementById(inputId);
  input.addEventListener("input", function () { onComboInput(inputId); });
  input.addEventListener("blur", function () { hideList(inputId); });
}
bindCombo("na-agent");
bindCombo("na-cwd");
document.getElementById("na-agent").addEventListener("change", fillNewCwd);
document.getElementById("na-model").addEventListener("change", function () {
  fillSelect("na-effort", effortsFor(document.getElementById("na-model").value), true, "medium");
});
document.addEventListener("mousedown", function (ev) {
  var node = ev.target;
  while (node && node !== document) {
    if (node.getAttribute && node.getAttribute("data-value") !== null &&
        node.parentNode && node.parentNode.id && node.parentNode.id.slice(-5) === "-list") {
      ev.preventDefault();
      pickOption(node);
      return;
    }
    node = node.parentNode;
  }
});
document.getElementById("new-form").addEventListener("submit", function (ev) {
  ev.preventDefault();
  launchNew(false);
});
(function () {
  var all = document.querySelectorAll("a.row");
  var i;
  selectedEl = all.length ? all[0] : null;
  for (i = 0; i < all.length; i++) {
    if (all[i].className.indexOf("selected") !== -1) selectedEl = all[i];
  }
  paintSort("working");
  paintSort("idle");
  paintSort("older");
  paintOlder();
  paintCounts();
  sync();
})();
</script>
</body>
</html>
"""
    choices = dashboard_cwd_choices(cwd_defaults)
    body = body.replace("@@AGENTS@@", script_json(list(agents)))
    body = body.replace("@@CWDS@@", script_json(choices))
    body = body.replace("@@CWD@@", script_json(cwd_defaults))
    body = body.replace("@@OLDER_WINDOW@@", str(OLDER_WINDOW))
    body = body.replace("@@OLDER_STEP@@", str(OLDER_STEP))
    body = body.replace("@@VERSION@@", html.escape(VERSION))
    return body.replace("@@ROWS@@", "\n".join(blocks))


def bind_choices():
    # The addresses the Host check already treats as this machine. IPv4
    # only: the listener is an AF_INET socket.
    out = ["127.0.0.1", "0.0.0.0"]
    for addr in sorted(interface_ipv4s()):
        if addr not in out:
            out.append(addr)
    return out


def running_sessions():
    with LIVE_LOCK:
        return sum(1 for live in LIVE.values() if live.alive())


def settings_overrides(args):
    notes = []
    if args is None:
        return notes
    if getattr(args, "cli_bind", None):
        notes.append("--bind %s on the command line wins over the saved bind." % args.cli_bind)
    if getattr(args, "cli_port", None) is not None:
        notes.append("--port %s on the command line wins over the saved port." % args.cli_port)
    elif getattr(args, "env_port", None) is not None:
        notes.append("ERGOSPHERE_PORT=%s wins over the saved port." % args.env_port)
    return notes


def settings_state(server):
    config = server.config
    agents = dict(server.cwd_defaults)
    host, port = server.server_address[0], server.server_address[1]
    return {
        "bind": config.get("bind") or "",
        "port": config.get("port") or "",
        "runningBind": host,
        "runningPort": port,
        "binds": bind_choices(),
        "agents": agents,
        "names": sorted(set(list_agents()) | set(agents)),
        "sessions": running_sessions(),
        "overrides": settings_overrides(getattr(server, "args", None)),
    }


def settings_page(state):
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Settings - Ergosphere</title>
<style>
body { background: #000; color: #fff; font: 14px/1.45 ui-monospace, monospace; margin: 0; padding: 0.6rem 0.8rem 3rem; }
a, a:visited, a:hover, a:active { color: #fff; }
h2 { font-size: 14px; font-weight: normal; margin: 0.4rem 0; }
input, button, select { font: inherit; color: #fff; background: #000; border: 1px solid #fff; }
input:focus, button:focus, select:focus, a:focus { outline: 1px solid #fff; }
button { cursor: pointer; padding: 0 0.5rem; }
button:hover, button:focus { background: #fff; color: #000; }
button:disabled, button:disabled:hover { color: #777; border-color: #777; background: #000; cursor: default; }
.bar { height: 1px; background: #fff; width: 100%; margin: 0.6rem 0; }
#corner { position: absolute; top: 0.6rem; right: 0.8rem; font-size: 11px; line-height: 1.45; color: #fff; }
.line { display: flex; flex-wrap: wrap; align-items: center; gap: 0.6rem; margin: 0.45rem 0; }
.note { margin: 0.3rem 0; }
.note:empty { display: none; }
table { border-collapse: collapse; width: 100%; max-width: 60rem; }
td, th { text-align: left; padding: 0.15rem 0.6rem 0.15rem 0; font-weight: normal; vertical-align: middle; }
td.path input { width: 100%; box-sizing: border-box; }
#port { width: 7ch; }
#add-path { flex: 1 1 24rem; min-width: 12rem; }
</style>
</head>
<body>
<div id="corner"><span id="version">v@@VERSION@@</span></div>
<div><a href="/">&larr; Session List</a></div>
<h2>Server</h2>
<div class="note" id="running"></div>
<div class="line"><label>Bind <select id="bind"></select></label><label>Port <input id="port" type="number" min="1" max="65535"></label><button type="button" id="save-server">Save</button></div>
<div class="note">0.0.0.0 or a network address lets anyone who can reach this port run grok as this user. There is no login.</div>
<div class="note" id="overrides"></div>
<div class="note" id="server-msg"></div>
<div class="bar"></div>
<h2>Agents</h2>
<table><thead><tr><th>Agent</th><th>Working directory</th><th></th></tr></thead><tbody id="agents"></tbody></table>
<div class="line"><select id="add-name"></select><input id="add-path" placeholder="/absolute/existing/directory" autocomplete="off"><button type="button" id="add">Add</button></div>
<div class="note" id="agent-msg"></div>
<div class="bar"></div>
<h2>Restart</h2>
<div class="note" id="sessions"></div>
<div class="line"><button type="button" id="restart">Restart Ergosphere</button></div>
<div class="note" id="restart-msg"></div>
<script>
var state = @@STATE@@;
var shownPort = "";
function el(id) { return document.getElementById(id); }
function say(id, text) { el(id).textContent = text || ""; }
function post(body) {
  return fetch("/settings", {
    method: "POST",
    cache: "no-store",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body)
  }).then(function (resp) {
    return resp.json().then(function (data) {
      return {ok: resp.ok, status: resp.status, data: data || {}};
    }, function () {
      return {ok: false, status: resp.status, data: {}};
    });
  }, function () {
    return {ok: false, status: 0, data: {error: "request failed"}};
  });
}
function fillOptions(sel, values, current) {
  var i, opt;
  sel.textContent = "";
  for (i = 0; i < values.length; i++) {
    opt = document.createElement("option");
    opt.value = values[i];
    opt.textContent = values[i];
    sel.appendChild(opt);
  }
  if (current) sel.value = current;
}
function renderServer() {
  var binds = state.binds.slice();
  if (state.bind && binds.indexOf(state.bind) === -1) binds.push(state.bind);
  fillOptions(el("bind"), binds, state.bind || "127.0.0.1");
  // No saved port: show the one in use, and do not save it unless changed.
  el("port").value = String(state.port || state.runningPort);
  shownPort = el("port").value;
  say("running", "Running on " + state.runningBind + ":" + state.runningPort + ".");
  say("overrides", state.overrides.join(" "));
}
function renderAgents() {
  var body = el("agents");
  var names = Object.keys(state.agents).sort();
  var free = [];
  var i, tr, td, input, save, remove;
  body.textContent = "";
  for (i = 0; i < names.length; i++) {
    tr = document.createElement("tr");
    td = document.createElement("td");
    td.textContent = names[i];
    tr.appendChild(td);
    td = document.createElement("td");
    td.className = "path";
    input = document.createElement("input");
    input.value = state.agents[names[i]];
    input.setAttribute("autocomplete", "off");
    input.setAttribute("data-name", names[i]);
    td.appendChild(input);
    tr.appendChild(td);
    td = document.createElement("td");
    save = document.createElement("button");
    save.type = "button";
    save.textContent = "Save";
    save.setAttribute("data-act", "set");
    save.setAttribute("data-name", names[i]);
    remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = "Remove";
    remove.setAttribute("data-act", "remove");
    remove.setAttribute("data-name", names[i]);
    td.appendChild(save);
    td.appendChild(document.createTextNode(" "));
    td.appendChild(remove);
    tr.appendChild(td);
    body.appendChild(tr);
  }
  for (i = 0; i < state.names.length; i++) {
    if (!Object.prototype.hasOwnProperty.call(state.agents, state.names[i])) free.push(state.names[i]);
  }
  fillOptions(el("add-name"), free, "");
  el("add").disabled = !free.length;
}
function renderRestart() {
  say("sessions", "Grok sessions running in this server: " + state.sessions + ". Restart needs zero.");
  el("restart").disabled = state.sessions !== 0;
}
function agentResult(res) {
  if (res.ok && res.data.agents) {
    state.agents = res.data.agents;
    if (res.data.names) state.names = res.data.names;
    renderAgents();
    say("agent-msg", "Saved. New sessions use it now.");
  } else {
    say("agent-msg", res.data.error || "could not save");
  }
}
function setAgent(name, path) {
  return post({action: "agent-set", name: name, path: path}).then(agentResult);
}
el("agents").addEventListener("click", function (ev) {
  var btn = ev.target;
  if (!btn || !btn.getAttribute) return;
  var act = btn.getAttribute("data-act");
  var name = btn.getAttribute("data-name");
  if (!act || !name) return;
  if (act === "remove") {
    post({action: "agent-remove", name: name}).then(agentResult);
    return;
  }
  var inputs = el("agents").querySelectorAll("input");
  var i;
  for (i = 0; i < inputs.length; i++) {
    if (inputs[i].getAttribute("data-name") === name) setAgent(name, inputs[i].value.trim());
  }
});
el("add").addEventListener("click", function () {
  var name = el("add-name").value;
  var path = el("add-path").value.trim();
  if (!name) return;
  setAgent(name, path).then(function () {
    if (Object.prototype.hasOwnProperty.call(state.agents, name)) el("add-path").value = "";
  });
});
el("save-server").addEventListener("click", function () {
  var body = {action: "server", bind: el("bind").value};
  var raw = el("port").value;
  var port;
  if (state.port || raw !== shownPort) {
    port = parseInt(raw, 10);
    body.port = isNaN(port) ? 0 : port;
  }
  post(body).then(function (res) {
    if (res.ok) {
      state.bind = res.data.bind;
      state.port = res.data.port;
      say("server-msg", "Saved. Applies after restart.");
    } else {
      say("server-msg", res.data.error || "could not save");
    }
  });
});
function waitFor(origin, oldStart, tries) {
  var url = origin + "/health";
  var same = origin === location.origin;
  fetch(url, same ? {cache: "no-store"} : {cache: "no-store", mode: "no-cors"}).then(function (resp) {
    if (!same) return true;
    return resp.json().then(function (data) { return data && data.started !== oldStart; });
  }).then(function (up) {
    if (up) {
      location.href = origin + "/settings";
      return;
    }
    throw new Error("old");
  }).catch(function () {
    if (tries <= 0) {
      say("restart-msg", "Ergosphere did not come back at " + origin + ".");
      return;
    }
    if (same) {
      setTimeout(function () { waitFor(origin, oldStart, tries - 1); }, 500);
      return;
    }
    // The new port may have been taken; the server then falls back here.
    fetch("/health", {cache: "no-store"}).then(function (resp) { return resp.json(); }).then(function (data) {
      if (data && data.started !== oldStart) {
        location.href = location.origin + "/settings";
        return;
      }
      throw new Error("old");
    }).catch(function () {
      setTimeout(function () { waitFor(origin, oldStart, tries - 1); }, 500);
    });
  });
}
el("restart").addEventListener("click", function () {
  el("restart").disabled = true;
  say("restart-msg", "Restarting...");
  fetch("/health", {cache: "no-store"}).then(function (resp) { return resp.json(); }).then(function (health) {
    return post({action: "restart"}).then(function (res) {
      if (!res.ok) {
        say("restart-msg", res.data.error || "could not restart");
        if (typeof res.data.sessions === "number") {
          state.sessions = res.data.sessions;
          renderRestart();
        } else {
          el("restart").disabled = false;
        }
        return;
      }
      var host = location.hostname;
      var bind = res.data.bind;
      if (bind && bind !== "0.0.0.0" && bind !== state.runningBind) host = bind;
      if (host.indexOf(":") !== -1) host = "[" + host + "]";
      waitFor(location.protocol + "//" + host + ":" + res.data.port, health.started, 60);
    });
  }).catch(function () {
    say("restart-msg", "could not restart");
    el("restart").disabled = false;
  });
});
renderServer();
renderAgents();
renderRestart();
</script>
</body>
</html>
""".replace("@@VERSION@@", html.escape(VERSION)).replace("@@STATE@@", script_json(state))


def chat_pick_page():
    # A visit without an existing session does not open a terminal and does not start grok.
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Ergosphere</title>
<style>
html, body { height: 100%; margin: 0; background: #111; color: #ddd; font: 14px sans-serif; }
#bar { height: 28px; display: flex; gap: 0.8rem; align-items: center; padding: 0 0.6rem; }
#bar a { color: #ddd; }
#pick { margin: 0.8rem 1rem; }
#version { position: absolute; top: 0; right: 0.6rem; height: 28px; line-height: 28px; font-size: 11px; color: #ddd; }
</style>
</head>
<body>
<div id="bar"><a href="/">← Session List</a></div>
<div id="version">v@@VERSION@@</div>
<p id="pick">The session list is where a session is chosen.</p>
<script>
document.getElementById("pick").hidden = false;
</script>
</body>
</html>
""".replace("@@VERSION@@", html.escape(VERSION))


def chat_page(resume="", holder=None, running=False, title=""):
    # Full-height xterm. Leaving this page drops the socket and does not
    # ask the server to kill grok. A reload or a second window reattaches.
    # running: this server already has the grok process. Only then does the
    # page connect on load. A stopped session waits for the Resume action.
    # A resume that already has a holder must show the notice in this HTML.
    # The quit button cannot wait for a websocket frame to learn the id.
    held = isinstance(holder, dict)
    can_quit = bool(held and holder_can_quit(holder))
    sid_js = resume if isinstance(resume, str) and SID_TOKEN_RE.match(resume) else ""
    if held:
        busy_css = (
            "#busy { display: block; flex: 1; overflow: auto; "
            "padding: 0.8rem 1rem; white-space: pre-wrap; }"
        )
        term_css = "#term { flex: 0 0 0; height: 0; min-height: 0; overflow: hidden; }"
        status = "session is already running"
        notice = busy_notice(sid_js, holder)
        visible_sid = sid_js
    else:
        busy_css = "#busy { display: none; padding: 0.8rem 1rem; white-space: pre-wrap; }"
        term_css = "#term { flex: 1; min-height: 0; }"
        status = "connecting" if running else "Ended"
        notice = ""
        visible_sid = "" if running else sid_js
    button_attr = "" if can_quit else " hidden"
    hold_js = "true" if held else "false"
    run_js = "true" if running and not held else "false"
    if not isinstance(title, str):
        title = ""
    title = " ".join(title.split())[:180]
    title_attr = "" if title else " hidden"
    gap_attr = "" if title else " hidden"
    doc_title = (title + " - Ergosphere") if title else "Ergosphere"
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>@@DOC_TITLE@@</title>
<link rel="stylesheet" href="/vendor/xterm.css">
<style>
html, body { height: 100%; margin: 0; background: #111; color: #ddd; font: 14px sans-serif; }
#bar { height: 28px; display: flex; gap: 0.8rem; align-items: center; padding: 0 0.6rem; }
#bar a { color: #ddd; }
#version { position: absolute; top: 0; right: 0.6rem; height: 28px; line-height: 28px; font-size: 11px; color: #ddd; }
#bar { padding-right: 4rem; }
#stop { font: 14px/1.45 ui-monospace, monospace; color: #fff; background: #000; border: 1px solid #fff; padding: 0 0.5rem; cursor: pointer; }
#stop:hover, #stop:focus { background: #fff; color: #000; outline: none; }
#resume-ask { position: absolute; z-index: 6; left: 50%; top: 50%; transform: translate(-50%, -50%); width: 36rem; max-width: calc(100% - 2rem); box-sizing: border-box; background: #000; color: #fff; border: 1px solid #fff; padding: 0.8rem 1rem; font: 14px/1.45 ui-monospace, monospace; }
#resume-ask[hidden] { display: none; }
#resume-ask button { font: inherit; color: #fff; background: #000; border: 1px solid #fff; margin: 0.6rem 0.6rem 0 0; }
#resume-ask button:focus { background: #fff; color: #000; }
#wrap { height: calc(100vh - 28px); display: flex; }
#stage { position: relative; flex: 1; min-width: 0; height: 100%; display: flex; flex-direction: column; }
@@BUSY_CSS@@
#busy button { font: 14px/1.45 ui-monospace, monospace; color: #fff; background: #000; border: 1px solid #fff; padding: 0 0.5rem; cursor: pointer; margin-top: 0.6rem; }
#busy button:hover, #busy button:focus { background: #fff; color: #000; outline: none; }
@@TERM_CSS@@
</style>
</head>
<body>
<div id="bar"><a href="/">← Session List</a> | <button id="stop" type="button">Quit</button> | <span id="status">@@STATUS@@</span> | <span id="title"@@TITLE_ATTR@@>@@TITLE@@</span><span id="title-gap"@@GAP_ATTR@@> | </span><span id="sid">Session ID: @@SID_TEXT@@</span></div>
<div id="version">v@@VERSION@@</div>
<div id="wrap"><div id="stage"><div id="resume-ask" hidden><div>Resume this session?</div><button type="button" id="resume-yes">Resume</button><button type="button" id="resume-no">Cancel</button></div><div id="busy"><div id="busy-text">@@NOTICE@@</div><button id="take" type="button"@@BUTTON_ATTR@@>Quit that CLI and resume here</button></div><div id="term"></div></div></div>
<script src="/vendor/xterm.js"></script>
<script src="/vendor/addon-fit.js"></script>
<script>
var term = new Terminal({cursorBlink: true, fontFamily: "monospace", fontSize: 14, scrollback: 5000});
// Ctrl+V is xterm's clipboard chord (byte 0x16). Returning false skips that
// and leaves the event so the browser paste, the same path as Ctrl+Shift+V,
// supplies text/plain. clipboard.read is refused on this HTTP page. A frozen
// session or the resume dialog still swallows the key.
var swallowCopy = false;
var suppressSend = false;
function copyTermText(text) {
  if (window.isSecureContext && navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
    try {
      var pending = navigator.clipboard.writeText(text);
      if (pending && typeof pending.catch === "function") pending.catch(function () {});
      return;
    } catch (err) {}
  }
  // execCommand copies the textarea only while it is focused. xterm would
  // report that focus change to grok. Hold those bytes; this key is a copy.
  suppressSend = true;
  var area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.style.position = "fixed";
  area.style.left = "0";
  area.style.top = "0";
  area.style.width = "1px";
  area.style.height = "1px";
  area.style.opacity = "0";
  document.body.appendChild(area);
  try {
    area.focus();
    area.select();
    try { document.execCommand("copy"); } catch (err) {}
  } finally {
    document.body.removeChild(area);
    try { term.focus(); } catch (err) {}
    suppressSend = false;
  }
}
// Ctrl+C copies when text is selected and is not sent. No selection returns
// true so xterm sends 0x03. Ctrl+Shift+C copies the same way. Ctrl+V is
// unchanged: returning false skips xterm's 0x16 and the browser pastes.
term.attachCustomKeyEventHandler(function (ev) {
  if (!ev || (ev.type !== "keydown" && ev.type !== "keypress")) return true;
  // ev.code is the physical key, so a non-Latin layout still copies. A Latin
  // layout uses the letter it prints: Dvorak's KeyC is j, and Ctrl+J is sent.
  var isC = ev.key === "c" || ev.key === "C" ||
    (ev.code === "KeyC" && !/^[A-Za-z]$/.test(ev.key || ""));
  if (ev.type === "keydown") swallowCopy = false;
  if (isC && ev.ctrlKey && !ev.altKey && !ev.metaKey) {
    if (ev.type === "keypress" && swallowCopy) {
      swallowCopy = false;
      ev.preventDefault();
      return false;
    }
    // A selection of blank cells has nothing to copy, so it interrupts.
    var copied = "";
    try { if (term.hasSelection()) copied = term.getSelection(); } catch (err) { copied = ""; }
    if (typeof copied !== "string") copied = "";
    if (copied.trim()) {
      if (ev.type === "keydown") {
        copyTermText(copied);
        try { term.clearSelection(); } catch (err) {}
        swallowCopy = true;
      }
      ev.preventDefault();
      return false;
    }
  }
  if (!ev.ctrlKey || ev.shiftKey || ev.altKey || ev.metaKey) return true;
  if (ev.key !== "v" && ev.key !== "V") return true;
  if (ended || resumeAskOpen()) ev.preventDefault();
  return false;
});
var fit = new FitAddon.FitAddon();
var sock = null;
var opened = false;
var stopping = false;
var ended = false;
var sid = @@SID_JS@@;
var hold = @@HOLD_JS@@;
var running = @@RUN_JS@@;
var termOpened = false;
var sawOutput = false;
var sessionReady = false;
var failText = "";
var waitTimer = null;
var waitGen = 0;
function showTerm() {
  var termEl = document.getElementById("term");
  termEl.style.flex = "1 1 auto";
  termEl.style.height = "";
  termEl.style.minHeight = "0";
  termEl.style.overflow = "hidden";
}
function ensureTerm() {
  showTerm();
  if (termOpened) return;
  termOpened = true;
  term.loadAddon(fit);
  term.open(document.getElementById("term"));
  // Edge draws a writing-hint dot on the focused field's caret; xterm's
  // hidden helper textarea sits at the terminal cursor, so turn hints off.
  if (term.textarea) term.textarea.setAttribute("writingsuggestions", "false");
  armEndedPrompt();
}
function hideTerm() {
  var termEl = document.getElementById("term");
  termEl.style.flex = "0 0 0px";
  termEl.style.height = "0";
}
term.onData(function (data) {
  if (suppressSend || ended || !sock || sock.readyState !== 1) return;
  sock.send(new TextEncoder().encode(data));
});
term.onResize(function (size) {
  if (!sock || sock.readyState !== 1 || !size) return;
  sock.send(JSON.stringify({type: "resize", cols: size.cols, rows: size.rows}));
});
term.onSelectionChange(function () {
  var text = "";
  try { text = term.getSelection(); } catch (err) { return; }
  if (!text) return;
  try {
    var clip = navigator.clipboard;
    if (!clip || typeof clip.writeText !== "function") return;
    var pending = clip.writeText(text);
    if (pending && typeof pending.catch === "function") pending.catch(function () {});
  } catch (err) {}
});
// Mouse modes are stripped, so xterm never encodes the wheel. A notch is
// still the SGR report mode 1006 would have produced: button 64 up, 65 down.
// A left click that stays in one cell is the same encoder's press (M) and
// release (m). A cell change is a drag: no bytes, so the browser keeps it.
var wheelPartial = 0;
function wheelReport(deltaY, col, row, ctrl, alt, shift) {
  var action = deltaY < 0 ? 0 : 1;
  var code = 0;
  if (ctrl) code |= 16;
  if (shift) code |= 4;
  if (alt) code |= 8;
  code |= 64;
  code |= action;
  return String.fromCharCode(27) + "[<" + code + ";" + col + ";" + row + "M";
}
function sgrClick(button, action, col, row, ctrl, alt, shift) {
  var code = 0;
  if (ctrl) code |= 16;
  if (shift) code |= 4;
  if (alt) code |= 8;
  if (button === 4) {
    code |= 64;
    code |= action;
  } else {
    code |= button & 3;
    if (action === 32) code |= 32;
  }
  var finalCh = (action === 0 && button !== 4) ? "m" : "M";
  return String.fromCharCode(27) + "[<" + code + ";" + col + ";" + row + finalCh;
}
function clickReports(down, up) {
  if (!down || !up || down.moved) return "";
  if (down.button !== 0 || up.button !== 0) return "";
  if (down.shift || up.shift || down.alt || up.alt) return "";
  if (down.col !== up.col || down.row !== up.row) return "";
  if (!(down.col >= 1) || !(down.row >= 1)) return "";
  return sgrClick(0, 1, down.col, down.row, down.ctrl, false, false) + sgrClick(0, 0, up.col, up.row, up.ctrl, false, false);
}
function wheelLines(ev) {
  if (!ev || !ev.deltaY || ev.shiftKey) return 0;
  var amount = ev.deltaY;
  var rows = term.rows || 24;
  if (ev.deltaMode === 2) return amount * rows;
  if (ev.deltaMode === 0) {
    var screen = document.querySelector("#term .xterm-screen");
    var rowH = 17;
    if (screen) {
      var rect = screen.getBoundingClientRect();
      if (rect.height > 0 && rows > 0) rowH = rect.height / rows;
    }
    amount = amount / rowH;
    wheelPartial += amount;
    var lines = Math.floor(Math.abs(wheelPartial));
    if (wheelPartial < 0) lines = -lines;
    wheelPartial = wheelPartial % 1;
    return lines;
  }
  return amount;
}
function wheelCell(ev) {
  var cols = term.cols || 80;
  var rows = term.rows || 24;
  var col = 0;
  var row = 0;
  var screen = document.querySelector("#term .xterm-screen");
  if (screen && ev && typeof ev.clientX === "number") {
    var rect = screen.getBoundingClientRect();
    if (rect.width > 0) col = Math.floor((ev.clientX - rect.left) / (rect.width / cols));
    if (rect.height > 0) row = Math.floor((ev.clientY - rect.top) / (rect.height / rows));
  }
  if (col < 0) col = 0;
  if (row < 0) row = 0;
  if (col >= cols) col = cols - 1;
  if (row >= rows) row = rows - 1;
  return {col: col + 1, row: row + 1};
}
document.getElementById("term").addEventListener("wheel", function (ev) {
  if (!wheelLines(ev)) return;
  ev.preventDefault();
  ev.stopPropagation();
  if (ended || !sock || sock.readyState !== 1) return;
  var cell = wheelCell(ev);
  sock.send(new TextEncoder().encode(wheelReport(ev.deltaY, cell.col, cell.row, ev.ctrlKey, ev.altKey, ev.shiftKey)));
}, {capture: true, passive: false});
window.addEventListener("resize", function () {
  if (!termOpened) return;
  try { fit.fit(); } catch (err) {}
});
function resumeAskOpen() {
  return !document.getElementById("resume-ask").hidden;
}
function openResumeAsk() {
  var box = document.getElementById("resume-ask");
  if (!box.hidden) return;
  box.hidden = false;
  document.getElementById("resume-yes").focus();
}
function closeResumeAsk() {
  document.getElementById("resume-ask").hidden = true;
}
function armEndedPrompt() {
  var helper = document.querySelector("#term .xterm-helper-textarea");
  if (!helper || helper.getAttribute("data-resume-ask") === "1") return;
  helper.setAttribute("data-resume-ask", "1");
  function openFromHelper(ev) {
    if (!ended || resumeAskOpen()) return;
    ev.preventDefault();
    ev.stopPropagation();
    openResumeAsk();
  }
  helper.addEventListener("click", openFromHelper, true);
  helper.addEventListener("focus", openFromHelper, true);
}
document.getElementById("resume-ask").addEventListener("keydown", function (ev) {
  if (ev.key !== "Escape") return;
  ev.preventDefault();
  ev.stopPropagation();
  closeResumeAsk();
}, true);
document.getElementById("resume-yes").addEventListener("click", function () {
  if (!resumeAskOpen()) return;
  closeResumeAsk();
  resumeSession();
});
document.getElementById("resume-no").addEventListener("click", function () {
  closeResumeAsk();
});
document.getElementById("term").addEventListener("click", function (ev) {
  if (!ended) {
    if (termOpened) term.focus();
    return;
  }
  if (resumeAskOpen()) return;
  ev.preventDefault();
  ev.stopPropagation();
  openResumeAsk();
}, true);
var clickDown = null;
document.getElementById("term").addEventListener("mousedown", function (ev) {
  clickDown = null;
  if (ev.button !== 0 || ev.shiftKey || ev.altKey) return;
  var cell = wheelCell(ev);
  clickDown = {col: cell.col, row: cell.row, button: 0, ctrl: !!ev.ctrlKey, alt: false, shift: false, moved: false};
}, true);
document.addEventListener("mousemove", function (ev) {
  if (!clickDown || clickDown.moved || (ev.buttons & 1) === 0) return;
  var cell = wheelCell(ev);
  if (cell.col !== clickDown.col || cell.row !== clickDown.row) clickDown.moved = true;
}, true);
document.addEventListener("mouseup", function (ev) {
  var down = clickDown;
  clickDown = null;
  if (!down || ev.button !== 0) return;
  var termEl = document.getElementById("term");
  if (!termEl || !termEl.contains(ev.target)) return;
  var cell = wheelCell(ev);
  var bytes = clickReports(down, {col: cell.col, row: cell.row, button: 0, ctrl: !!ev.ctrlKey, alt: !!ev.altKey, shift: !!ev.shiftKey});
  if (!bytes || ended || stopping || !sock || sock.readyState !== 1) return;
  sock.send(new TextEncoder().encode(bytes));
}, true);
function setStatus(text, active) {
  var el = document.getElementById("status");
  el.textContent = text;
  el.style.color = active ? "#00ff00" : "";
}
function setTitle(text) {
  var el = document.getElementById("title");
  var gap = document.getElementById("title-gap");
  var title = "";
  if (typeof text === "string") title = text;
  if (title.length > 180) title = title.slice(0, 180);
  if (!title) {
    el.hidden = true;
    gap.hidden = true;
    el.textContent = "";
    document.title = "Ergosphere";
    return;
  }
  el.hidden = false;
  gap.hidden = false;
  el.textContent = title;
  document.title = title + " - Ergosphere";
}
function pollTitle() {
  if (!sid) return;
  fetch("/session-info?id=" + encodeURIComponent(sid), {cache: "no-store"}).then(function (resp) {
    if (!resp.ok) return null;
    return resp.json();
  }).then(function (data) {
    if (!data || typeof data.title !== "string") return;
    setTitle(data.title);
  }).catch(function () {});
}
// The title poll runs only while the session is not Ended.
var titleTimer = null;
function startTitlePoll() {
  if (!titleTimer) titleTimer = setInterval(pollTitle, 5000);
}
function stopTitlePoll() {
  if (titleTimer) { clearInterval(titleTimer); titleTimer = null; }
}
function setSid(id) {
  if (!id) return;
  document.getElementById("sid").textContent = "Session ID: " + id;
  pollTitle();
}
function showEnded() {
  ended = true;
  stopTitlePoll();
  waitGen += 1;
  if (waitTimer) { clearTimeout(waitTimer); waitTimer = null; }
  setStatus("Ended", false);
  // SGR 2 on "Loading session". This xterm's dim of default #ffffff is #ffffff80.
  // Over the default background #000000 that paints #808080.
  document.getElementById("status").style.color = "#808080";
  document.getElementById("stop").textContent = "Resume";
}
function resumeSession() {
  ended = false;
  startTitlePoll();
  stopping = false;
  sawOutput = false;
  sessionReady = false;
  failText = "";
  waitGen += 1;
  if (waitTimer) { clearTimeout(waitTimer); waitTimer = null; }
  document.getElementById("stop").textContent = "Quit";
  setStatus("starting", false);
  var old = sock;
  sock = null;
  if (old) {
    old.onclose = function () {};
    if (old.readyState < 2) {
      try { old.close(); } catch (err) {}
    }
  }
  // Only this request starts a stopped session. /term attaches only.
  // held: another grok owns it; connect so /term shows that notice.
  fetch("/resume", {
    method: "POST",
    cache: "no-store",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({id: sid})
  }).then(function (resp) {
    return resp.json().then(function (data) {
      return {ok: resp.ok, data: data};
    }, function () {
      return {ok: false, data: null};
    });
  }).then(function (res) {
    var data = res.data || {};
    if (res.ok || data.held === true) {
      connect();
      return;
    }
    showEnded();
    setStatus(typeof data.error === "string" && data.error ? data.error : "could not resume", false);
  }).catch(function () {
    showEnded();
    setStatus("could not resume", false);
  });
}
function markOutput() {
  if (hold || !sessionReady || ended) return;
  sawOutput = true;
  waitGen += 1;
  if (waitTimer) { clearTimeout(waitTimer); waitTimer = null; }
  if (!stopping) setStatus("Active", true);
}
function armOutputWait() {
  if (sawOutput || hold || waitTimer) return;
  var gen = waitGen;
  waitTimer = setTimeout(function () {
    waitTimer = null;
    if (gen !== waitGen || sawOutput || hold || stopping || ended) return;
    var why = failText;
    if (!why) {
      var open = sock && sock.readyState === 1;
      why = open ? "grok sent no output within 15 seconds" : "terminal socket failed";
    }
    setStatus(why, false);
  }, 15000);
}
function frameText(data) {
  if (typeof data === "string") return data;
  try { return new TextDecoder().decode(new Uint8Array(data)); } catch (err) { return ""; }
}
function oneLine(text) {
  var out = "";
  var pending = false;
  for (var i = 0; i < text.length; i++) {
    var code = text.charCodeAt(i);
    if (code === 32 || code === 9 || code === 10 || code === 13) {
      pending = true;
      continue;
    }
    if (pending && out) out += " ";
    pending = false;
    out += text.charAt(i);
  }
  return out;
}
function writeTerm(data) {
  if (typeof data === "string") term.write(data);
  else term.write(new Uint8Array(data));
}
function connect() {
  ensureTerm();
  try { fit.fit(); } catch (err) {}
  sessionReady = false;
  failText = "";
  armOutputWait();
  var cols = term.cols || 80;
  var rows = term.rows || 24;
  var q = new URLSearchParams();
  q.set("cols", String(cols));
  q.set("rows", String(rows));
  if (sid) q.set("resume", sid);
  var url = (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/term?" + q.toString();
  var current = new WebSocket(url);
  current.binaryType = "arraybuffer";
  sock = current;
  current.onopen = function () {
    if (sock !== current) return;
    opened = true;
    if (stopping) {
      setStatus("quitting", false);
      current.send(JSON.stringify({type: "stop"}));
      return;
    }
    if (sawOutput && !ended) setStatus("Active", true);
    term.focus();
  };
  current.onmessage = function (ev) {
    if (sock !== current) return;
    if (typeof ev.data === "string") {
      try {
        var msg = JSON.parse(ev.data);
        if (msg && msg.type === "ended") {
          showEnded();
          return;
        }
        if (msg && msg.type === "session" && msg.id) {
          sessionReady = true;
          sid = msg.id;
          setSid(msg.id);
          return;
        }
        if (msg && msg.type === "status" && msg.text) {
          setStatus(msg.text, false);
          if (msg.text === "session is already running") {
            hold = true;
            waitGen += 1;
            if (waitTimer) { clearTimeout(waitTimer); waitTimer = null; }
            if (msg.id) sid = msg.id;
            var busy = document.getElementById("busy");
            document.getElementById("busy-text").textContent = msg.notice || msg.text;
            var btn = document.getElementById("take");
            if (msg.canQuit) {
              btn.hidden = false;
              btn.disabled = false;
              btn.textContent = msg.button || "Quit that CLI and resume here";
            } else {
              btn.hidden = true;
            }
            busy.style.display = "block";
            hideTerm();
          }
          if (msg.id) setSid(msg.id);
          return;
        }
        if (msg && msg.type) return;
      } catch (err) {}
    }
    if (!sessionReady || hold) {
      var early = oneLine(frameText(ev.data));
      if (early && !hold) {
        failText = early.slice(0, 300);
        setStatus(failText, false);
      }
      writeTerm(ev.data);
      return;
    }
    markOutput();
    writeTerm(ev.data);
  };
  current.onerror = function () {
    if (sock !== current) return;
    if (!failText) failText = "terminal socket failed";
    if (!hold) setStatus(failText, false);
  };
  current.onclose = function () {
    if (sock !== current) return;
    if (ended) {
      showEnded();
      return;
    }
    if (stopping) {
      showEnded();
      return;
    }
    if (hold) {
      setStatus("session is already running", false);
      return;
    }
    if (!sawOutput) {
      setStatus(failText || "terminal socket failed", false);
      return;
    }
    if (sid) {
      setStatus("reconnecting", false);
      setTimeout(connect, 400);
      return;
    }
    setStatus("detached", false);
  };
}
document.getElementById("stop").addEventListener("click", function () {
  if (ended) {
    resumeSession();
    return;
  }
  stopping = true;
  setStatus("quitting", false);
  if (sock && sock.readyState === 1) sock.send(JSON.stringify({type: "stop"}));
});
document.getElementById("take").addEventListener("click", function () {
  if (!sid || !hold) return;
  var btn = document.getElementById("take");
  btn.disabled = true;
  taking = true;
  setStatus("quitting that CLI", false);
  fetch("/take?id=" + encodeURIComponent(sid), {method: "POST", cache: "no-store"}).then(function (resp) {
    return resp.json().then(function (data) {
      return {ok: resp.ok, data: data};
    });
  }).then(function (res) {
    if (!res.ok || !res.data || !res.data.ok) {
      var why = (res.data && res.data.error) || "could not quit that CLI";
      setStatus(why, false);
      document.getElementById("busy-text").textContent =
        document.getElementById("busy-text").textContent + "\\n" + why;
      btn.disabled = false;
      taking = false;
      return;
    }
    taking = false;
    openAfterHolder(true);
  }).catch(function () {
    setStatus("could not quit that CLI", false);
    btn.disabled = false;
    taking = false;
  });
});
var taking = false;
function openAfterHolder(start) {
  if (!hold) return;
  hold = false;
  document.getElementById("busy").style.display = "none";
  try { if (termOpened) term.reset(); } catch (err) {}
  if (start) {
    resumeSession();
    return;
  }
  ensureTerm();
  showEnded();
}
function pollHold() {
  if (!hold || !sid || stopping || taking) return;
  fetch("/holder?id=" + encodeURIComponent(sid), {cache: "no-store"}).then(function (resp) {
    return resp.json();
  }).then(function (data) {
    if (!hold || stopping || taking) return;
    if (!data || data.held !== false) return;
    openAfterHolder(false);
  }).catch(function () {});
}
setInterval(pollHold, 1000);
startTitlePoll();
if (sid) pollTitle();
if (hold) pollHold();
if (!hold && running) { ensureTerm(); requestAnimationFrame(connect); }
else if (!hold) { ensureTerm(); setSid(sid); showEnded(); }
</script>
</body>
</html>
""".replace("@@BUSY_CSS@@", busy_css).replace("@@TERM_CSS@@", term_css).replace(
        "@@STATUS@@", html.escape(status)
    ).replace("@@TITLE_ATTR@@", title_attr).replace(
        "@@GAP_ATTR@@", gap_attr
    ).replace("@@SID_TEXT@@", html.escape(visible_sid)).replace(
        "@@NOTICE@@", html.escape(notice)
    ).replace("@@BUTTON_ATTR@@", button_attr).replace(
        "@@SID_JS@@", json.dumps(sid_js)
    ).replace("@@HOLD_JS@@", hold_js).replace("@@RUN_JS@@", run_js).replace(
        "@@TITLE@@", html.escape(title)
    ).replace("@@DOC_TITLE@@", html.escape(doc_title)).replace(
        "@@VERSION@@", html.escape(VERSION)
    )


# Linux SIOCGIFADDR from linux/sockios.h. One IPv4 address per interface.
_SIOCGIFADDR = 0x8915


def interface_ipv4s():
    """Configured IPv4 addresses. Names are not resolved."""
    found = set()
    try:
        ifaces = socket.if_nameindex()
    except OSError:
        return found
    for _index, name in ifaces:
        if not isinstance(name, str) or not name:
            continue
        sock = None
        try:
            encoded = name.encode("utf-8")[:15]
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            res = fcntl.ioctl(sock.fileno(), _SIOCGIFADDR, struct.pack("256s", encoded))
            found.add(socket.inet_ntoa(res[20:24]))
        except (OSError, UnicodeError):
            continue
        finally:
            if sock is not None:
                sock.close()
    return found


def interface_ipv6s():
    """Configured IPv6 addresses from the kernel table. Names are not resolved."""
    found = set()
    try:
        raw = Path("/proc/net/if_inet6").read_text(encoding="ascii")
    except (OSError, UnicodeError):
        return found
    for line in raw.splitlines():
        parts = line.split()
        if not parts or len(parts[0]) != 32:
            continue
        try:
            packed = bytes.fromhex(parts[0])
            found.add(socket.inet_ntop(socket.AF_INET6, packed))
        except (ValueError, OSError):
            continue
    return found


def listener_names(bound_host):
    names = {"localhost", "127.0.0.1", "::1"}
    if isinstance(bound_host, str) and bound_host:
        names.add(bound_host.lower())
        # A wildcard listener is reached by this machine's own addresses.
        # A DNS name is still not resolved, and a specific bind stays narrow.
        if bound_host.strip().lower() == "0.0.0.0":
            for addr in interface_ipv4s():
                names.add(addr.lower())
            for addr in interface_ipv6s():
                names.add(addr.lower())
    return names


def split_host_port(value):
    """Return (name, port) from a Host header. The port is required.

    The name is not resolved. A rebinding DNS name must not become allowed
    just because it points at this machine.
    """
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw or any(ch in raw for ch in " \t\r\n,\\@"):
        return None
    if raw.startswith("["):
        end = raw.find("]")
        if end < 2:
            return None
        host = raw[1:end]
        rest = raw[end + 1:]
        if not host or "/" in host or not rest.startswith(":"):
            return None
        port_text = rest[1:]
    else:
        if raw.count(":") != 1:
            return None
        host, port_text = raw.split(":", 1)
        if not host or "/" in host:
            return None
    if not port_text.isdigit() or len(port_text) > 5:
        return None
    port = int(port_text)
    if port < 1 or port > 65535:
        return None
    return host, port


class WsReject(RuntimeError):
    """A client frame or message is refused. Close with this code and reason."""

    def __init__(self, code, reason):
        super().__init__(reason)
        self.code = code


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        return

    def handle_one_request(self):
        # Every response write (_json, _html, _vendor, _redirect, frames) runs
        # inside this. A client that resets mid-response is normal; log one
        # line instead of a traceback and let the server keep serving.
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError) as exc:
            self.close_connection = True
            print(
                "client gone %s %s: %s" % (
                    getattr(self, "command", None) or "-",
                    urlparse(getattr(self, "path", "") or "").path or "-",
                    exc.__class__.__name__,
                ),
                flush=True,
            )

    def _authorized(self):
        token = self.server.token
        if not token:
            return True
        header = self.headers.get("Authorization", "")
        prefix = "Bearer "
        if not header.startswith(prefix):
            return False
        # http.server decodes header bytes as latin-1; compare raw bytes.
        # compare_digest raises TypeError on non-ASCII str, so never pass str.
        try:
            given = header[len(prefix):].encode("latin-1")
        except UnicodeEncodeError:
            return False
        return hmac.compare_digest(given, token.encode("utf-8"))

    def _json(self, code, obj):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)

    def _html(self, body):
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        # No framing: a foreign page must not overlay clicks on Resume/Start.
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _bound_endpoint(self):
        addr = self.server.server_address
        if not isinstance(addr, tuple) or len(addr) < 2:
            return "", 0
        host, port = addr[0], addr[1]
        if not isinstance(host, str) or type(port) is not int:
            return "", 0
        return host, port

    def _name_allowed(self, host):
        bound_host, _port = self._bound_endpoint()
        if not isinstance(host, str) or not host:
            return False
        return host.lower() in listener_names(bound_host)

    def _host_allowed(self):
        # Loopback names and the bound address only. Do not trust
        # X-Forwarded-* and do not treat "Origin host equals Host" as proof.
        values = self.headers.get_all("Host") or []
        if len(values) != 1:
            return False
        parsed = split_host_port(values[0])
        if parsed is None:
            return False
        host, port = parsed
        _bound_host, bound_port = self._bound_endpoint()
        if port != bound_port:
            return False
        return self._name_allowed(host)

    def _origin_allowed(self):
        values = self.headers.get_all("Origin") or []
        if len(values) != 1 or not isinstance(values[0], str):
            return False
        origin = values[0].strip()
        if not origin or origin.lower() == "null":
            return False
        if any(ch in origin for ch in "\r\n\t ,"):
            return False
        try:
            parsed = urlparse(origin)
            oport = parsed.port
        except ValueError:
            return False
        if parsed.username is not None or parsed.password is not None:
            return False
        scheme = "https" if self.server.tls else "http"
        if parsed.scheme != scheme:
            return False
        if parsed.path not in ("",) or parsed.params or parsed.query or parsed.fragment:
            return False
        _bound_host, bound_port = self._bound_endpoint()
        if oport != bound_port:
            return False
        return self._name_allowed(parsed.hostname)

    def _foreign_fetch(self):
        # A GET that spawns a process must not be triggerable from another
        # page (an <img> or no-cors fetch sends no Origin). Browsers label
        # those with Sec-Fetch-Site; another port on the same host is
        # "same-site", so only same-origin and none (typed URL) pass.
        # Header-free clients such as curl pass. A present Origin must match.
        site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if site and site not in ("same-origin", "none"):
            return True
        if self.headers.get_all("Origin") and not self._origin_allowed():
            return True
        return False

    def _guard(self, state_change=False, spawn_get=False):
        if not self._host_allowed():
            self._json(403, {"error": "forbidden"})
            return False
        if not self._authorized():
            self._json(401, {"error": "unauthorized"})
            return False
        if state_change and not self._origin_allowed():
            self._json(403, {"error": "forbidden"})
            return False
        if spawn_get and self._foreign_fetch():
            self._json(403, {"error": "forbidden"})
            return False
        return True

    def do_GET(self):
        path = urlparse(self.path).path
        if not self._guard(path == "/term", path == "/models"):
            return
        if path == "/health":
            bound = self.server.server_address
            self._json(200, {
                "ok": True,
                "port": bound[1],
                "tls": self.server.tls,
                "product": "ergosphere",
                "pid": os.getpid(),
                "bind": bound[0],
                "code_sha256": getattr(self.server, "code_sha256", None),
                "version": VERSION,
                # A restart keeps the pid (exec), so this tells the new
                # process from the old one.
                "started": getattr(self.server, "started", None),
            })
            return
        if path == "/":
            _total, sessions = load_sessions(None)
            self._html(dashboard_page(sessions, list_agents(), self.server.cwd_defaults))
            return
        if path == "/dashboard":
            self._redirect("/")
            return
        if path == "/settings":
            self._html(settings_page(settings_state(self.server)))
            return
        if path == "/models":
            models, err = grok_models_list()
            self._json(200, {
                "ok": not err,
                "error": err,
                "models": models,
                "efforts": list(DEFAULT_EFFORTS),
                "agents": list_agents(),
                "last": self.server.config.get("last") or {},
            })
            return
        if path == "/session-info":
            self._session_info()
            return
        if path == "/subagents":
            self._subagents()
            return
        if path == "/holder":
            self._holder()
            return
        if path == "/chat":
            # approval, agent, and cwd on this URL are not a launch.
            qs = parse_qs(urlparse(self.path).query, keep_blank_values=False)
            resume = one_query(qs, "resume")
            if not isinstance(resume, str) or not SID_TOKEN_RE.match(resume):
                self._html(chat_pick_page())
                return
            with LIVE_LOCK:
                live = LIVE.get(resume)
                running = live is not None and live.alive()
            holder = None if running else session_holder(resume)
            saved = find_session(resume)
            page_title = saved.get("title") if isinstance(saved, dict) else ""
            if not isinstance(page_title, str):
                page_title = ""
            self._html(chat_page(resume, holder, running, page_title))
            return
        if path in VENDOR_FILES:
            self._vendor(path)
            return
        if path == "/term":
            self._term()
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if not self._guard(path in ("/take", "/launch", "/resume", "/settings")):
            return
        if path == "/settings":
            self._settings()
            return
        if path == "/take":
            self._take()
            return
        if path == "/launch":
            self._launch()
            return
        if path == "/resume":
            self._resume()
            return
        self._json(404, {"error": "not found"})

    def _change_config(self, change):
        # Re-read the file so an edit made while the server runs is kept.
        with CONFIG_LOCK:
            path = config_path()
            if path.exists():
                data = check_config(read_json_file(path), str(path))
            else:
                data = check_config(copy.deepcopy(self.server.config), str(path))
            change(data)
            data = check_config(data, str(path))
            write_config(data)
            self.server.config = data
            self.server.cwd_defaults = dict(data["agents"])
            return data

    def _settings(self):
        # Origin and Host were checked before this. Nothing here runs a command.
        obj = self._json_object()
        if obj is None:
            self._json(400, {"ok": False, "error": "invalid request"})
            return
        action = obj.get("action")
        if action == "restart":
            self._settings_restart()
            return
        try:
            if action == "server":
                bind = obj.get("bind")
                port = obj.get("port")
                if bind not in bind_choices():
                    raise ValueError("bind must be one of " + ", ".join(bind_choices()))
                # A missing port leaves the saved port (or its absence) alone.
                if port is not None and not valid_port(port):
                    raise ValueError("port must be a whole number from 1 to 65535")
                problem = self._endpoint_problem(bind, port)
                if problem:
                    self._json(409, {"ok": False, "error": problem + "; not saved"})
                    return

                def change(data):
                    data["bind"] = bind
                    if port is not None:
                        data["port"] = port
                data = self._change_config(change)
                self._json(200, {"ok": True, "bind": data["bind"], "port": data.get("port") or ""})
                return
            if action in ("agent-set", "agent-remove"):
                name = obj.get("name")
                if not isinstance(name, str) or not NAME_RE.match(name):
                    raise ValueError("unknown agent")
                if action == "agent-set":
                    path = obj.get("path")
                    if name not in list_agents() and name not in self.server.cwd_defaults:
                        raise ValueError("unknown agent")
                    if not existing_dir(path):
                        raise ValueError("path must be an existing absolute directory")

                    def change(data):
                        data["agents"][name] = path
                else:
                    if name not in self.server.cwd_defaults:
                        raise ValueError("unknown agent")

                    def change(data):
                        data["agents"].pop(name, None)
                data = self._change_config(change)
                print("settings %s %s" % (action, name), flush=True)
                self._json(200, {
                    "ok": True,
                    "agents": data["agents"],
                    "names": sorted(set(list_agents()) | set(data["agents"])),
                })
                return
        except ValueError as exc:
            self._json(400, {"ok": False, "error": str(exc)[:300]})
            return
        except OSError as exc:
            self._json(500, {"ok": False, "error": "cannot write %s (%s)" % (CONFIG_NAME, exc.strerror or exc)})
            return
        self._json(400, {"ok": False, "error": "unknown action"})

    def _endpoint_problem(self, bind, port):
        # Our own listener holds the running port, so only another port is
        # probed. A port taken later is still covered by the exec fallback.
        _host, running_port = self._bound_endpoint()
        if port is None or port == running_port:
            return ""
        return endpoint_problem(bind, port)

    def _settings_restart(self):
        # Only with zero grok sessions. RESTART stops new launches until exec.
        with LIVE_LOCK:
            busy = sum(1 for live in LIVE.values() if live.alive())
            if not busy:
                RESTART.set()
        if busy:
            self._json(409, {
                "ok": False,
                "sessions": busy,
                "error": "%d grok %s running; restart needs zero" % (busy, "session" if busy == 1 else "sessions"),
            })
            return
        nxt = resolve_endpoint(argparse.Namespace(**vars(self.server.args)), self.server.config)
        problem = self._endpoint_problem(nxt.bind, nxt.port)
        if problem:
            RESTART.clear()
            self._json(409, {"ok": False, "error": problem + "; not restarting"})
            return
        print("restart requested; next %s:%s" % (nxt.bind, nxt.port), flush=True)
        self._json(200, {"ok": True, "bind": nxt.bind, "port": nxt.port})
        self.close_connection = True
        threading.Thread(target=self.server.shutdown, daemon=True).start()

    def _drain_body(self):
        raw = self.headers.get("Content-Length")
        if not raw:
            return
        try:
            n = int(raw)
        except ValueError:
            return
        if n <= 0 or n > 65536:
            return
        try:
            self._exact(n)
        except (EOFError, OSError):
            return

    def _field_str(self, obj, key):
        if key not in obj or obj[key] is None:
            return ""
        value = obj[key]
        if not isinstance(value, str):
            raise ValueError("invalid request")
        return value

    def _json_object(self):
        raw_len = self.headers.get("Content-Length")
        if not isinstance(raw_len, str) or not raw_len.isdigit() or len(raw_len) > 5:
            return None
        n = int(raw_len)
        if n < 2 or n > 65536:
            return None
        try:
            raw = self._exact(n)
        except (EOFError, OSError):
            return None
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError):
            return None
        if not isinstance(obj, dict):
            return None
        return obj

    def _launch(self):
        # The new-session action. Host and Origin were checked before this.
        # A page load does not call it. The returned id is an existing process.
        obj = self._json_object()
        if obj is None:
            self._json(400, {"ok": False, "error": "invalid request"})
            return
        try:
            agent = self._field_str(obj, "agent")
            cwd = self._field_str(obj, "cwd")
            approval = self._field_str(obj, "approval")
            model = self._field_str(obj, "model")
            effort = self._field_str(obj, "effort")
            agent, cwd = new_launch(
                agent,
                approval,
                cwd,
                self.server.cwd_defaults,
                os.getcwd(),
            )
            model, effort = checked_model_effort(model, effort)
            sid = str(uuid.uuid4())
            cmd = grok_argv(agent, approval, cwd, None, sid, model, effort)
            live, _reattach = open_live(sid, cmd, cwd, 80, 24)
        except (ValueError, RuntimeError, OSError) as exc:
            self._json(400, {"ok": False, "error": str(exc)[:300]})
            return
        if model or effort:
            self._remember_last(model, effort)
        self._json(200, {"ok": True, "id": live.sid})

    def _remember_last(self, model, effort):
        # The launch already happened; a failed save is logged, not returned.
        def change(data):
            data["last"] = {"model": model, "effort": effort}
        try:
            self._change_config(change)
        except (ValueError, OSError) as exc:
            print("could not save last model and effort: %s" % exc, flush=True)

    def _resume(self):
        # The Resume action for a saved session. Host and Origin were checked
        # before this. Loading /chat or opening /term never starts one.
        obj = self._json_object()
        sid = obj.get("id") if obj is not None else None
        if not isinstance(sid, str) or not SID_RE.match(sid):
            self._json(400, {"ok": False, "error": "unknown session"})
            return
        with LIVE_LOCK:
            live = LIVE.get(sid)
            running = live is not None and live.alive()
        # Reply after release: a slow client must not hold LIVE_LOCK.
        if running:
            self._json(200, {"ok": True, "id": sid})
            return
        if session_holder(sid):
            self._json(409, {"ok": False, "held": True, "error": "session is already running"})
            return
        row = find_session(sid)
        if row is None:
            self._json(404, {"ok": False, "error": "unknown session"})
            return
        cwd = row.get("cwd")
        if not existing_dir(cwd):
            self._json(400, {"ok": False, "error": "session cwd is not an existing absolute directory"})
            return
        try:
            cmd = grok_argv(None, None, cwd, sid)
            open_live(sid, cmd, cwd, 80, 24)
        except (RuntimeError, OSError) as exc:
            self._json(400, {"ok": False, "error": str(exc)[:300]})
            return
        self._json(200, {"ok": True, "id": sid})

    def _take(self):
        # The button posts this. It does not run on connect. Quit only a
        # cmdline cli holder. A grok with an ergosphere.py ancestor is a web
        # session and is not signalled. The page then posts /resume.
        # Only a UUID-shaped id with a session directory on disk is taken.
        self._drain_body()
        qs = parse_qs(urlparse(self.path).query, keep_blank_values=False)
        sid = one_query(qs, "id") or one_query(qs, "resume")
        if not SID_TOKEN_RE.match(sid) or session_dir_for(sid) is None:
            self._json(400, {"ok": False, "error": "unknown session"})
            return
        ok, info = release_holder(sid)
        if not ok and info.get("error") == "no holder":
            self._json(404, info)
            return
        self._json(200 if ok else 409, info)

    def _session_info(self):
        qs = parse_qs(urlparse(self.path).query, keep_blank_values=False)
        want = one_query(qs, "id")
        if not want:
            self._json(200, {"sessions": list_live()})
            return
        if not SID_TOKEN_RE.match(want):
            self._json(400, {"error": "unknown session"})
            return
        saved = find_session(want)
        live_row = None
        for item in list_live():
            if item.get("id") == want:
                live_row = item
                break
        if saved is None and live_row is None:
            self._json(404, {"error": "not found"})
            return
        if saved is None:
            body = dict(live_row)
            body["title"] = ""
        else:
            body = dict(saved)
            if live_row is not None:
                body["pid"] = live_row.get("pid")
                body["alive"] = live_row.get("alive")
                body["started"] = live_row.get("started")
        self._json(200, body)

    def _holder(self):
        qs = parse_qs(urlparse(self.path).query, keep_blank_values=False)
        sid = one_query(qs, "id") or one_query(qs, "resume")
        if not SID_TOKEN_RE.match(sid):
            self._json(400, {"ok": False, "error": "unknown session"})
            return
        holder = session_holder(sid)
        if not holder:
            self._json(200, {"ok": True, "held": False, "id": sid})
            return
        self._json(200, {
            "ok": True,
            "held": True,
            "id": sid,
            "pid": holder["pid"],
            "via": holder["via"],
            "kind": holder.get("kind") or "cli",
        })

    def _subagents(self):
        qs = parse_qs(urlparse(self.path).query, keep_blank_values=False)
        sid = one_query(qs, "id")
        with LIVE_LOCK:
            live = LIVE.get(sid)
        if live is None:
            self._json(404, {"error": "not found"})
            return
        self._json(200, {
            "id": sid,
            "pid": live.proc.pid,
            "alive": live.proc.poll() is None,
            "subagents": live.subagent_rows(),
        })

    def _vendor(self, path):
        name, ctype = VENDOR_FILES[path]
        file_path = vendor_dir() / name
        try:
            data = file_path.read_bytes()
        except OSError:
            self._json(404, {"error": "not found"})
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _exact(self, n):
        buf = bytearray()
        while len(buf) < n:
            chunk = self.rfile.read(n - len(buf))
            if not chunk:
                raise EOFError("closed")
            buf += chunk
        return bytes(buf)

    def _send_frame(self, opcode, payload):
        n = len(payload)
        header = bytearray([0x80 | (opcode & 0x0F)])
        if n < 126:
            header.append(n)
        elif n < 65536:
            header.append(126)
            header += struct.pack("!H", n)
        else:
            header.append(127)
            header += struct.pack("!Q", n)
        with self.ws_lock:
            if self.ws_closed:
                raise EOFError("closed")
            self.wfile.write(header + payload)
            self.wfile.flush()

    def _read_frame(self):
        hdr = self._exact(2)
        b0, b1 = hdr[0], hdr[1]
        opcode = b0 & 0x0F
        fin = (b0 & 0x80) != 0
        masked = (b1 & 0x80) != 0
        if not masked:
            # RFC 6455 5.1: a server must close on an unmasked client frame.
            raise WsReject(1002, "client frames must be masked")
        length = b1 & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._exact(8))[0]
        if length > FRAME_MAX:
            raise WsReject(1009, "frame too large")
        mask = self._exact(4) if masked else b""
        payload = self._exact(length) if length else b""
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return opcode, payload, fin

    def _resolve_attach(self):
        # Query approval, agent, and cwd are not a launch. POST /launch starts
        # a new session and POST /resume a saved one. /term only attaches.
        qs = parse_qs(urlparse(self.path).query, keep_blank_values=False)
        cols = dim_arg(one_query(qs, "cols"), 80)
        rows = dim_arg(one_query(qs, "rows"), 24)
        sid = one_query(qs, "resume")
        if not SID_RE.match(sid):
            raise ValueError("unknown session")
        with LIVE_LOCK:
            live = LIVE.get(sid)
            have = live is not None and live.alive()
        if not have and find_session(sid) is None:
            raise ValueError("unknown session")
        return sid, cols, rows

    def _term(self):
        # Upgrade does not set Connection: close. Do not read a second request.
        # The socket stays open with no idle timeout. Closing it detaches only.
        # An unknown id is rejected before the upgrade. This never starts grok:
        # a stopped saved session gets an ended frame and waits for /resume.
        self.close_connection = True
        try:
            sid, cols, rows = self._resolve_attach()
        except ValueError as exc:
            self._json(404, {"error": str(exc)[:300]})
            return
        key = self.headers.get("Sec-WebSocket-Key", "")
        version = self.headers.get("Sec-WebSocket-Version", "")
        if not key or version != "13":
            self._json(400, {"error": "websocket required"})
            return
        accept = base64.b64encode(
            hashlib.sha1((key + WS_GUID).encode("ascii")).digest()
        ).decode("ascii")
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.close_connection = True
        try:
            self.connection.settimeout(None)
        except OSError:
            pass
        try:
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        self.ws_lock = threading.Lock()
        self.ws_closed = False
        live = None
        client = None
        try:
            with LIVE_LOCK:
                existing = LIVE.get(sid)
                have_live = existing is not None and existing.alive()
            if have_live:
                live = existing
            else:
                holder = session_holder(sid)
                if holder:
                    notice = busy_notice(sid, holder)
                    can_quit = holder_can_quit(holder)
                    self._send_frame(2, notice.replace("\n", "\r\n").encode("utf-8"))
                    note = json.dumps({
                        "type": "status",
                        "text": "session is already running",
                        "id": sid,
                        "pid": holder["pid"],
                        "via": holder["via"],
                        "kind": holder.get("kind") or "cli",
                        "canQuit": can_quit,
                        "button": BUTTON_LABEL if can_quit else "",
                        "notice": notice,
                    })
                    self._send_frame(1, note.encode("utf-8"))
                    print(
                        "term busy sid=%s holder=%s via=%s" % (sid, holder["pid"], holder["via"]),
                        flush=True,
                    )
                    return
                self._send_frame(1, json.dumps({"type": "ended", "id": sid}).encode("utf-8"))
                print("term stopped sid=%s not started" % sid, flush=True)
                return
            print("term reattach pid=%s sid=%s" % (live.proc.pid, sid), flush=True)
            note = json.dumps({"type": "session", "id": sid, "reattach": True})
            self._send_frame(1, note.encode("utf-8"))
            client = live.attach(self)
            live.repaint(cols, rows)
            self._read_input(live)
        finally:
            self.ws_closed = True
            if live is not None:
                live.detach(client)
            if client is not None and client.sender is not None:
                client.sender.join(timeout=2)
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _read_input(self, live):
        frag_op = None
        frag = b""
        try:
            while not STOP.is_set():
                opcode, payload, fin = self._read_frame()
                if opcode == 9:
                    self._send_frame(0xA, payload)
                    continue
                if opcode == 10:
                    continue
                if opcode == 8:
                    break
                if opcode in (1, 2):
                    frag_op = opcode
                    frag = payload
                elif opcode == 0 and frag_op is not None:
                    # Continuations count toward one message; cap the total.
                    if len(frag) + len(payload) > FRAME_MAX:
                        raise WsReject(1009, "message too large")
                    frag += payload
                else:
                    continue
                if not fin:
                    continue
                data = frag
                op = frag_op
                frag_op = None
                frag = b""
                if op == 1:
                    self._on_text(live, data)
                else:
                    live.write(data)
        except WsReject as exc:
            print("term close sid=%s code=%s: %s" % (live.sid, exc.code, exc), flush=True)
            try:
                self._send_frame(8, struct.pack("!H", exc.code) + str(exc).encode("utf-8"))
            except (EOFError, OSError):
                pass
            return
        except (EOFError, OSError, RuntimeError, ValueError):
            return

    def _on_text(self, live, data):
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            live.write(data)
            return
        try:
            msg = json.loads(text)
        except ValueError:
            live.write(data)
            return
        if isinstance(msg, dict) and msg.get("type") == "stop":
            # Stop is the only client message that kills this PTY.
            live.shutdown()
            with LIVE_LOCK:
                if LIVE.get(live.sid) is live:
                    LIVE.pop(live.sid, None)
            print("term stop pid=%s sid=%s" % (live.proc.pid, live.sid), flush=True)
            raise EOFError("stopped")
        if isinstance(msg, dict) and msg.get("type") == "resize":
            live.resize(msg.get("cols"), msg.get("rows"))
            return
        live.write(data)


def bound_is_loopback(addr):
    try:
        return ipaddress.ip_address(addr).is_loopback
    except ValueError:
        return False


def bind_identity(host):
    """Loopback names the socket treats as 127.0.0.1.

    0.0.0.0 is not an alias of 127.0.0.1. A wildcard request must not
    count as a loopback server, or the reverse.
    """
    if not isinstance(host, str):
        return ""
    text = host.strip().lower()
    if text == "localhost":
        return "127.0.0.1"
    return text


def _health_body_ok(data, host, port, tls):
    if not isinstance(data, dict):
        return False
    if data.get("ok") is not True:
        return False
    if type(data.get("product")) is not str or data.get("product") != "ergosphere":
        return False
    # bool is a subclass of int. JSON true must not count as port 1 or pid 1.
    if type(data.get("port")) is not int or data.get("port") != port:
        return False
    if data.get("tls") is not (True if tls else False):
        return False
    if type(data.get("bind")) is not str or bind_identity(data.get("bind")) != bind_identity(host):
        return False
    pid = data.get("pid")
    if type(pid) is not int or pid <= 0:
        return False
    return True


def inspect_health(host, port, tls, token=None):
    # 0.0.0.0 is not a URL host that reaches the listener.
    # The bearer stays in the header. "match" is this server at this exposure.
    # "down" means nothing accepted the connection. Anything else answered,
    # or failed an authenticated probe, and is not already running.
    probe = "127.0.0.1" if host == "0.0.0.0" else host
    conn = None
    try:
        if tls:
            ctx = ssl._create_unverified_context()
            conn = http.client.HTTPSConnection(probe, port, timeout=1, context=ctx)
        else:
            conn = http.client.HTTPConnection(probe, port, timeout=1)
        headers = {}
        if token:
            headers["Authorization"] = "Bearer " + token
        conn.request("GET", "/health", headers=headers)
        resp = conn.getresponse()
        status = resp.status
        raw = resp.read(8192)
    except ConnectionRefusedError:
        return "down"
    except TimeoutError:
        return "reject"
    except http.client.HTTPException:
        return "reject"
    except OSError as exc:
        if exc.errno == errno.ECONNREFUSED:
            return "down"
        if exc.errno in (errno.ECONNRESET, errno.EPIPE, errno.ETIMEDOUT):
            return "reject"
        return "error"
    except (ValueError, UnicodeError):
        return "error"
    finally:
        if conn is not None:
            conn.close()
    if status != 200:
        return "reject"
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return "reject"
    if _health_body_ok(data, host, port, tls):
        return "match"
    return "reject"


def health_matches(host, port, tls, token=None):
    return inspect_health(host, port, tls, token) == "match"


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Ergosphere")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--bind", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--tls-cert", default=None)
    parser.add_argument("--tls-key", default=None)
    args = parser.parse_args(argv)
    if (args.tls_cert is None) != (args.tls_key is None):
        parser.error("--tls-cert and --tls-key are both required")
    # Remember what the command line and environment set, so ergosphere.json
    # only fills the gaps and the settings page can say what overrides it.
    args.cli_bind = args.bind
    args.cli_port = args.port
    args.env_port = None
    if args.port is None:
        raw = os.environ.get("ERGOSPHERE_PORT")
        if raw:
            try:
                args.env_port = int(raw)
            except ValueError:
                parser.error("ERGOSPHERE_PORT is not an integer")
            if not valid_port(args.env_port):
                parser.error("port out of range")
    elif not valid_port(args.port):
        parser.error("port out of range")
    return args


def resolve_endpoint(args, config):
    # --bind/--port, then ERGOSPHERE_PORT, then ergosphere.json, then defaults.
    args.bind = args.cli_bind or config.get("bind") or DEFAULT_HOST
    if args.cli_port is not None:
        args.port = args.cli_port
    elif args.env_port is not None:
        args.port = args.env_port
    else:
        args.port = config.get("port") or DEFAULT_PORT
    return args


def require_grok():
    """grok path, or None after printing the error. Shared by --check and start."""
    binary = grok_bin()
    if not binary:
        print("grok not on PATH", file=sys.stderr, flush=True)
    return binary


def cmd_check(args, binary):
    tls = 1 if args.tls_cert else 0
    total, _rows = load_sessions(limit=None)
    print("bind=%s:%s" % (args.bind, args.port), flush=True)
    print("tls=%s" % tls, flush=True)
    print("agents=%s" % len(list_agents()), flush=True)
    print("sessions=%s" % total, flush=True)
    print("vendor=%s" % len(VENDOR_FILES), flush=True)
    print("grok=%s" % binary, flush=True)
    return 0


class ErgosphereServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def runtime_fingerprint():
    root = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for name in ("ergosphere.py", "vendor/addon-fit.js", "vendor/xterm.css", "vendor/xterm.js"):
        digest.update(hashlib.sha256((root / name).read_bytes()).digest())
    return digest.hexdigest()


RESTART_FALLBACK_ENV = "ERGOSPHERE_RESTART_FALLBACK"


def endpoint_problem(bind, port):
    """"" if bind/port can be bound now, else why not. Same options as the server."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((bind, port))
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            return "%s:%s is already in use by another program" % (bind, port)
        return "cannot listen on %s:%s (%s)" % (bind, port, exc.strerror or exc)
    finally:
        sock.close()
    return ""


# listen() result when this server already answers there; a second start exits 0.
ALREADY_RUNNING = "already running"


def listen(args, tls, token):
    """The bound server, ALREADY_RUNNING, or None after printing why not."""
    seen = inspect_health(args.bind, args.port, tls, token)
    if seen == "match":
        print("already running", file=sys.stderr, flush=True)
        return ALREADY_RUNNING
    if seen == "reject":
        print(
            "not starting: %s:%s is in use and did not validate as ergosphere; left that listener running"
            % (args.bind, args.port),
            file=sys.stderr,
            flush=True,
        )
        return None
    try:
        return ErgosphereServer((args.bind, args.port), Handler)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            print(
                "not starting: %s:%s is in use and did not validate as ergosphere; left that listener running"
                % (args.bind, args.port),
                file=sys.stderr,
                flush=True,
            )
        else:
            print(
                "not starting: cannot listen on %s:%s (%s)"
                % (args.bind, args.port, exc.strerror or exc.errno),
                file=sys.stderr,
                flush=True,
            )
        return None


def serve(args, config):
    tls = bool(args.tls_cert)
    # Unset ERGOSPHERE_TOKEN means no bearer check, including --bind 0.0.0.0.
    # A set value is a path to a one-line token file such as ergosphere.token,
    # not the token itself. The file is read once here, at startup.
    if os.environ.get("ERGOSPHERE_TOKEN"):
        token = read_token_file()
    else:
        token = None
    httpd = listen(args, tls, token)
    # After a restart the previous endpoint is the fallback, so a port that
    # was taken between Save and Restart does not leave nothing listening.
    fallback = os.environ.pop(RESTART_FALLBACK_ENV, "")
    if httpd is None and fallback:
        host, _sep, port_text = fallback.rpartition(":")
        if valid_bind(host) and port_text.isdigit() and valid_port(int(port_text)):
            print(
                "restart: %s:%s is not available; falling back to %s:%s"
                % (args.bind, args.port, host, port_text),
                file=sys.stderr,
                flush=True,
            )
            args.bind, args.port = host, int(port_text)
            httpd = listen(args, tls, token)
    if httpd is ALREADY_RUNNING:
        return 0
    if httpd is None:
        return 1
    httpd.token = token
    httpd.tls = tls
    httpd.args = args
    httpd.started = int(time.time() * 1000)
    httpd.config = config
    # The agent map the launch path and the session list read. Settings
    # replaces this dict (never mutates it) so a reader sees old or new.
    httpd.cwd_defaults = dict(config["agents"])
    # Keep the fingerprint of the loaded code, not a later on-disk version.
    httpd.code_sha256 = runtime_fingerprint()
    if not token and not bound_is_loopback(httpd.server_address[0]):
        print(
            "warning: %s:%s is not loopback and has no token; anyone who can reach "
            "this port can run grok as this user" % (httpd.server_address[0], args.port),
            file=sys.stderr,
            flush=True,
        )
    if tls:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(args.tls_cert, args.tls_key)
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    print("ERGOSPHERE_READY port=%s tls=%s" % (args.port, 1 if tls else 0), flush=True)

    def _stop(_signum, _frame):
        STOP.set()
        kill_all()
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        httpd.serve_forever()
    finally:
        kill_all()
        httpd.server_close()
    if RESTART.is_set():
        return restart_self(httpd.server_address[0], httpd.server_address[1])
    return 0


def restart_self(old_bind, old_port):
    # Same interpreter and the same command line, so new settings apply.
    # The listener is already closed and sockets are not inherited.
    argv = list(getattr(sys, "orig_argv", None) or [sys.executable] + sys.argv)
    env = dict(os.environ)
    env[RESTART_FALLBACK_ENV] = "%s:%s" % (old_bind, old_port)
    print("restarting: %s (fallback %s:%s)" % (" ".join(argv), old_bind, old_port), flush=True)
    sys.stderr.flush()
    try:
        os.execve(sys.executable, argv, env)
    except OSError as exc:
        print("restart failed: %s" % (exc.strerror or exc), file=sys.stderr, flush=True)
        return 1


def main(argv):
    args = parse_args(argv)
    # --check only reports. It does not write the migrated config.
    config = load_config(migrate=not args.check)
    resolve_endpoint(args, config)
    require_vendor()
    # Before binding: a server without grok would listen but never open a terminal.
    binary = require_grok()
    if not binary:
        return 1
    if args.check:
        return cmd_check(args, binary)
    return serve(args, config)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
