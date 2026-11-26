#!/usr/bin/env python3
"""Quitting a holder grok signals only the process that was checked (#148).

term_holder checks a holder's command line, then sends SIGTERM.  If that
grok exits and its pid goes to another process in between, the signal must
not reach the new one.  The test makes that happen on purpose: it runs in its
own user and pid namespace (unshare), kills the stand-in grok, and starts a
bystander at the same pid with clone3 set_tid.  Every process it starts lives
in that namespace and dies with it.  No real grok is run.

Run: python3 -B tests/pid_reuse_test.py
ERGOSPHERE_CODE=<path to ergosphere.py> tests another copy (A/B).
Skips (exit 0) where unprivileged user namespaces are not allowed.
"""
import ctypes
import errno
import importlib.util
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CODE = os.environ.get("ERGOSPHERE_CODE") or str(ROOT / "ergosphere.py")
INNER = "ERGO_PID_REUSE_INNER"


def outer():
    unshare = shutil.which("unshare")
    probe = [unshare, "-Urpf", "--mount-proc", "true"] if unshare else None
    if not probe or subprocess.call(probe, stderr=subprocess.DEVNULL) != 0:
        print("SKIP: no unprivileged user and pid namespace here")
        return 0
    env = dict(os.environ, **{INNER: "1"})
    return subprocess.call([unshare, "-Urpf", "--mount-proc", sys.executable, "-B", __file__], env=env)


class CloneArgs(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in (
        "flags", "pidfd", "child_tid", "parent_tid", "exit_signal", "stack",
        "stack_size", "tls", "set_tid", "set_tid_size", "cgroup")]


LIBC = ctypes.CDLL(None, use_errno=True)
LIBC.syscall.restype = ctypes.c_long
SYS_CLONE3 = 435


def spawn_at(pid, argv, block=()):
    """Fork and exec argv as exactly pid (clone3 set_tid; needs our own pid namespace).

    block: signals the new process starts with blocked (the mask survives exec).
    """
    tid = (ctypes.c_int32 * 1)(pid)
    args = CloneArgs(exit_signal=signal.SIGCHLD, set_tid=ctypes.addressof(tid), set_tid_size=1)
    got = LIBC.syscall(SYS_CLONE3, ctypes.byref(args), ctypes.c_size_t(ctypes.sizeof(args)))
    if got == 0:
        try:
            signal.pthread_sigmask(signal.SIG_BLOCK, block)
            os.execv(argv[0], argv)
        finally:
            os._exit(127)
    if got < 0:
        err = ctypes.get_errno()
        raise OSError(err, "clone3 set_tid=%d: %s" % (pid, os.strerror(err)))
    return got


FAKE = ("import signal,sys\n"
        "signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))\n"
        "print('ready', flush=True)\n"
        "signal.pause()\n")


class Run:
    def __init__(self, erg, grok):
        self.erg = erg
        self.sid = str(uuid.uuid4())
        self.grok = subprocess.Popen([grok, "-c", FAKE, "--resume", self.sid],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.PIPE)
        assert self.grok.stdout.readline() == b"ready\n", "stand-in grok did not start"
        self.pid = self.grok.pid
        self.holder = {"pid": self.pid, "via": "cmdline", "kind": "cli"}
        self.raw = erg.read_proc_cmdline(self.pid)
        assert erg.grok_cmdline(self.raw) and erg.cmdline_contains_sid(self.raw, self.sid)
        self.bystander = None

    def swap(self):
        """The checked grok exits and an unrelated process gets its pid."""
        self.grok.kill()
        self.grok.wait()
        self.bystander = spawn_at(self.pid, ["/bin/sleep", "60"])
        assert self.bystander == self.pid, "bystander got pid %s, not %s" % (self.bystander, self.pid)
        deadline = time.time() + 5
        while not self.erg.read_proc_cmdline(self.pid).startswith(b"/bin/sleep"):
            assert time.time() < deadline, "bystander did not exec"
            time.sleep(0.01)

    def relaunch(self):
        """The checked grok exits and a new grok on the same session gets its pid (#191)."""
        self.grok.kill()
        self.grok.wait()
        # SIGTERM blocked: a signal sent to it stays pending, so the test reads
        # whether it was hit (ShdPnd) instead of racing its exit.
        self.bystander = spawn_at(self.pid, [self.grok.args[0], "-c", "import signal; signal.pause()",
                                             "--resume", self.sid], block=(signal.SIGTERM,))
        assert self.bystander == self.pid, "new grok got pid %s, not %s" % (self.bystander, self.pid)
        deadline = time.time() + 5
        while not self.erg.cmdline_contains_sid(self.erg.read_proc_cmdline(self.pid), self.sid):
            assert time.time() < deadline, "new grok did not exec"
            time.sleep(0.01)

    def relaunched_hit(self):
        """True if the relaunched grok got a SIGTERM (pending, it blocks it); then kill and reap it."""
        with open("/proc/%d/status" % self.bystander) as fh:
            pending = [int(line.split()[1], 16) for line in fh if line.startswith(("SigPnd:", "ShdPnd:"))]
        os.kill(self.bystander, signal.SIGKILL)
        os.waitpid(self.bystander, 0)
        return any(mask & (1 << (signal.SIGTERM - 1)) for mask in pending)

    def bystander_signal(self):
        """Kill the bystander; the signal that ended it (SIGTERM = it was hit)."""
        os.kill(self.bystander, signal.SIGKILL)
        _pid, status = os.waitpid(self.bystander, 0)
        return signal.Signals(os.WTERMSIG(status)).name

    def done(self):
        if self.grok.poll() is None:
            self.grok.kill()
            self.grok.wait()


def check_legit_quit(erg, grok):
    run = Run(erg, grok)
    try:
        ok, info = erg.term_holder(run.sid, run.holder, run.raw)
        code = run.grok.wait(timeout=10) if ok else run.grok.poll()
        if not ok or code != 0:
            return "checked grok not quit: %r, exit %s" % (info, code)
    finally:
        run.done()
    return None


def check_reused_before_signal(erg, grok):
    run = Run(erg, grok)
    run.swap()
    ok, info = erg.term_holder(run.sid, run.holder, run.raw)
    hit = run.bystander_signal()
    if hit == "SIGTERM":
        return "bystander at reused pid %s got SIGTERM (term_holder %r)" % (run.pid, info)
    return None


def check_reused_after_pidfd(erg, grok):
    # The pid is reused after the pidfd opened and the command line re-read.
    if not hasattr(erg, "open_pidfd"):
        return "no open_pidfd: the signal does not go through a pidfd"
    if erg.open_pidfd(os.getpid()) is None:
        return "pidfd unavailable on this host"
    run = Run(erg, grok)
    real_open, real_read = erg.open_pidfd, erg.read_proc_cmdline
    armed = []

    def open_pidfd(pid):
        fd = real_open(pid)
        armed.append(pid)
        return fd

    def read(pid):
        raw = real_read(pid)
        if armed and pid == armed[0]:
            armed.clear()
            run.swap()
        return raw

    erg.open_pidfd, erg.read_proc_cmdline = open_pidfd, read
    try:
        ok, info = erg.term_holder(run.sid, run.holder, run.raw)
    finally:
        erg.open_pidfd, erg.read_proc_cmdline = real_open, real_read
    if run.bystander is None:
        return "the swap did not happen (term_holder %r)" % (info,)
    hit = run.bystander_signal()
    if hit == "SIGTERM":
        return "bystander at reused pid %s got SIGTERM through the pidfd (term_holder %r)" % (run.pid, info)
    return None


def relaunched_after_pidfd(before_read):
    """A new grok on the same session takes the pid after the pidfd opened (#191).

    before_read: the relaunch happens before the re-read (it reads the new
    grok) or right after it (it read the old one).  Either way term_holder
    must not report ok while the new grok runs, and must not signal it.
    """
    def check(erg, grok):
        if erg.open_pidfd(os.getpid()) is None:
            return "pidfd unavailable on this host"
        run = Run(erg, grok)
        real_open, real_read = erg.open_pidfd, erg.read_proc_cmdline
        armed = []

        def open_pidfd(pid):
            fd = real_open(pid)
            armed.append(pid)
            return fd

        def read(pid):
            fire = armed and pid == armed[0]
            if fire:
                armed.clear()
                if before_read:
                    run.relaunch()
            raw = real_read(pid)
            if fire and not before_read:
                run.relaunch()
            return raw

        erg.open_pidfd, erg.read_proc_cmdline = open_pidfd, read
        try:
            ok, info = erg.term_holder(run.sid, run.holder, run.raw)
        finally:
            erg.open_pidfd, erg.read_proc_cmdline = real_open, real_read
        if run.bystander is None:
            return "the relaunch did not happen (term_holder %r)" % (info,)
        if run.relaunched_hit():
            return "new grok at reused pid %s was signalled (term_holder %r)" % (run.pid, info)
        if ok:
            return "term_holder reported %r while the new grok at pid %s ran" % (info, run.pid)
        return None
    return check


def check_exit_before_signal(erg, grok):
    # The checked grok exits on its own between the checks and the signal
    # (ESRCH through the pidfd): that is a quit, reported ok.
    run = Run(erg, grok)
    real_send = signal.pidfd_send_signal

    def send(fd, sig, *rest):
        run.grok.kill()
        run.grok.wait()
        return real_send(fd, sig, *rest)

    signal.pidfd_send_signal = send
    try:
        ok, info = erg.term_holder(run.sid, run.holder, run.raw)
    finally:
        signal.pidfd_send_signal = real_send
        run.done()
    if not ok:
        return "grok that exited before the signal not reported gone: %r" % (info,)
    return None


def check_exit_after_reread(erg, grok):
    # The checked grok quits right after the re-read, nothing takes its pid
    # (e.g. a second tab's /take got it first): that is a quit, reported ok.
    run = Run(erg, grok)
    real_open, real_read = erg.open_pidfd, erg.read_proc_cmdline
    armed = []

    def open_pidfd(pid):
        fd = real_open(pid)
        armed.append(pid)
        return fd

    def read(pid):
        raw = real_read(pid)
        if armed and pid == armed[0]:
            armed.clear()
            run.grok.kill()
            run.grok.wait()
        return raw

    erg.open_pidfd, erg.read_proc_cmdline = open_pidfd, read
    try:
        ok, info = erg.term_holder(run.sid, run.holder, run.raw)
    finally:
        erg.open_pidfd, erg.read_proc_cmdline = real_open, real_read
        run.done()
    if not ok:
        return "grok that quit after the re-read not reported gone: %r" % (info,)
    return None


def without_pidfd(check):
    def run(erg, grok):
        if not hasattr(erg, "open_pidfd"):
            return "no open_pidfd to switch off"
        real = erg.open_pidfd
        erg.open_pidfd = lambda pid: None
        try:
            return check(erg, grok)
        finally:
            erg.open_pidfd = real
    return run


def pidfd_signal_fails(err, kill_err=None):
    """pidfd_send_signal raises err (a seccomp filter: ENOSYS or EPERM); os.kill raises kill_err if set."""
    def wrap(check):
        def run(erg, grok):
            real_send, real_kill = signal.pidfd_send_signal, os.kill

            def send(fd, sig, *rest):
                raise OSError(err, os.strerror(err))

            def kill(pid, sig):
                if kill_err and sig == signal.SIGTERM:
                    raise PermissionError(kill_err, os.strerror(kill_err))
                return real_kill(pid, sig)

            signal.pidfd_send_signal, os.kill = send, kill
            try:
                return check(erg, grok)
            finally:
                signal.pidfd_send_signal, os.kill = real_send, real_kill
        return run
    return wrap


def check_other_user_refused(erg, grok):
    # The holder belongs to another user: every signal is EPERM, so it is refused.
    run = Run(erg, grok)
    try:
        ok, info = erg.term_holder(run.sid, run.holder, run.raw)
        alive = run.grok.poll() is None
        if ok or not alive or "cannot signal" not in str(info.get("error")):
            return "another user's grok: term_holder %r, still running %s" % (info, alive)
    finally:
        run.done()
    return None


CHECKS = [
    ("legit quit (pidfd)", check_legit_quit),
    ("pid reused before the signal", check_reused_before_signal),
    ("pid reused after the pidfd opened", check_reused_after_pidfd),
    ("same-session grok relaunched at the pid, re-read sees it", relaunched_after_pidfd(True)),
    ("same-session grok relaunched at the pid after the re-read", relaunched_after_pidfd(False)),
    ("same-session grok relaunched, re-read sees it (pidfd_send_signal EPERM by policy)",
     pidfd_signal_fails(errno.EPERM)(relaunched_after_pidfd(True))),
    ("checked grok exits just before the signal", check_exit_before_signal),
    ("checked grok quits right after the re-read", check_exit_after_reread),
    ("legit quit (no pidfd fallback)", without_pidfd(check_legit_quit)),
    ("pid reused before the signal (no pidfd fallback)", without_pidfd(check_reused_before_signal)),
    ("legit quit (pidfd_send_signal ENOSYS)", pidfd_signal_fails(errno.ENOSYS)(check_legit_quit)),
    ("legit quit (pidfd_send_signal EPERM by policy)", pidfd_signal_fails(errno.EPERM)(check_legit_quit)),
    ("another user's grok (every signal EPERM) refused",
     pidfd_signal_fails(errno.EPERM, errno.EPERM)(check_other_user_refused)),
]


def inner():
    tmp = tempfile.mkdtemp(prefix="ergo-pidreuse-")
    grok = os.path.join(tmp, "grok")
    os.symlink(sys.executable, grok)
    spec = importlib.util.spec_from_file_location("ergosphere", CODE)
    erg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(erg)
    failed = 0
    try:
        for name, check in CHECKS:
            why = check(erg, grok)
            print("%s: %s" % ("FAIL" if why else "ok", name + (" -- " + why if why else "")), flush=True)
            failed += bool(why)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("PASS" if not failed else "FAIL: %d of %d" % (failed, len(CHECKS)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(inner() if os.environ.get(INNER) == "1" else outer())
