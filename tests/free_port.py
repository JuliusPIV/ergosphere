"""Prints a free loopback port P for a test; with N given, P+1 .. P+N-1 are free too.

Run: python3 -I tests/free_port.py [N]
P is picked at random outside the kernel's ephemeral range when it can be, so
the source port of an outgoing connection never takes P or a neighbour before
the test binds it, and two test runs started together get different ports.
With no free run of ports outside that range, P comes from the OS (bind to
port 0) with its neighbours checked.  Exit 1 if nothing is found.
"""
import random
import socket
import sys

FORBIDDEN_PORTS = {9119, 9120, 9121, 9241, 9242}
LOW = 10000


def ephemeral_range():
    try:
        with open("/proc/sys/net/ipv4/ip_local_port_range") as fh:
            lo, hi = (int(v) for v in fh.read().split()[:2])
            return lo, hi
    except (OSError, ValueError):
        return 32768, 60999


def windows(count):
    """Port ranges [start, stop) for the first port, outside the ephemeral range, best first."""
    lo, hi = ephemeral_range()
    spans = ((LOW, lo - count + 1), (hi + 1, 65536 - count + 1), (1024, min(LOW, lo - count + 1)))
    return [(start, stop) for start, stop in spans if start < stop]


def all_free(first, count):
    socks = []
    try:
        for port in range(first, first + count):
            s = socket.socket()
            socks.append(s)
            s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        for s in socks:
            s.close()


def usable(first, count):
    return FORBIDDEN_PORTS.isdisjoint(range(first, first + count)) and all_free(first, count)


def os_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def find(count):
    for start, stop in windows(count):
        for _ in range(200):
            first = random.randrange(start, stop)
            if usable(first, count):
                return first
    for _ in range(200):
        first = os_port()
        if first + count <= 65536 and usable(first, count):
            return first
    return None


def main():
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    first = find(count)
    if first is None:
        print("free_port.py: no %d free loopback ports in a row found" % count, file=sys.stderr)
        return 1
    print(first)
    return 0


if __name__ == "__main__":
    sys.exit(main())
