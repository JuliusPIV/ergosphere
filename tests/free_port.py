"""Prints a free loopback port P for a test; with N given, P+1 .. P+N-1 are free too.

Run: python3 -I tests/free_port.py [N]
P is picked at random below the kernel's ephemeral range, so the source port
of an outgoing connection never takes P or a neighbour before the test binds
it, and two test runs started together get different ports.  Exit 1 if no
such run of ports is found.
"""
import random
import socket
import sys

FORBIDDEN_PORTS = {9119, 9120, 9121, 9241, 9242}
LOW = 20000


def ephemeral_low():
    try:
        with open("/proc/sys/net/ipv4/ip_local_port_range") as fh:
            return int(fh.read().split()[0])
    except (OSError, ValueError, IndexError):
        return 32768


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
    high = ephemeral_low() - count
    for _ in range(200):
        first = random.randrange(LOW, high)
        if FORBIDDEN_PORTS.isdisjoint(range(first, first + count)) and all_free(first, count):
            print(first)
            return 0
    print("free_port.py: no %d free ports in a row found in %d-%d" % (count, LOW, high), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
