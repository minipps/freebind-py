"""Use normal socket helpers while the opt-in process-wide patch is active."""

import argparse
import socket

from freebind import Source, patch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="source IPv4/IPv6 address or CIDR")
    parser.add_argument("--host", required=True, help="TCP peer hostname or address")
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--sticky", action="store_true", help="reuse one selected source address")
    parser.add_argument("--timeout", type=float, default=10)
    args = parser.parse_args()

    source = Source(args.source, mode="sticky" if args.sticky else "random")
    with patch(source):
        # This uses the standard library helper so socket.connect() goes through the patch.
        with socket.create_connection((args.host, args.port), timeout=args.timeout) as sock:
            sock.sendall(b"report\n")
            with sock.makefile("rb") as response:
                print(response.readline().decode(errors="replace").rstrip())


if __name__ == "__main__":
    main()
