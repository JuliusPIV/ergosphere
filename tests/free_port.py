"""Prints a free loopback port P for a test; with N given, P+1 .. P+N-1 are free too.

Run: python3 -I tests/free_port.py [N]
P is picked at random outside the kernel's ephemeral range (below it when
there is room, else above it), so the source port of an outgoing connection
never takes P or a neighbour before the test binds it, and two test runs
started together get different ports.  Exit 1 if no such run of ports is found.
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


def window(count):
    """First port range [start, stop) outside the ephemeral range with room for count ports."""
    lo, hi = ephemeral_range()
    for start, stop in ((LOW, lo - count + 1), (hi + 1, 65536 - count + 1), (1024, lo - count + 1)):
        if start < stop:
            return start, stop
    return None


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


def main():
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    span = window(count)
    if span is None:
        print("free_port.py: no room for %d ports outside the ephemeral range %d-%d" % ((count,) + ephemeral_range()),
              file=sys.stderr)
        return 1
    for _ in range(200):
        first = random.randrange(*span)
        if FORBIDDEN_PORTS.isdisjoint(range(first, first + count)) and all_free(first, count):
            print(first)
            return 0
    print("free_port.py: no %d free ports in a row found in %d-%d" % ((count,) + span), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
