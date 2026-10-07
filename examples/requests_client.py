"""Send one HTTP(S) request through the Requests Freebind adapter."""

import argparse

import requests

from freebind import Source
from freebind.requests import FreebindAdapter


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="source IPv4/IPv6 address or CIDR")
    parser.add_argument("--url", required=True)
    parser.add_argument("--fresh", action="store_true", help="close each connection after its response")
    parser.add_argument("--sticky", action="store_true", help="reuse one selected source address")
    parser.add_argument("--ca", help="CA bundle used to verify HTTPS")
    parser.add_argument("--timeout", type=float, default=10)
    args = parser.parse_args()

    source = Source(args.source, mode="sticky" if args.sticky else "random")
    with requests.Session() as client:
        client.trust_env = False
        for scheme in ("http://", "https://"):
            client.mount(scheme, FreebindAdapter(source, fresh=args.fresh))
        with client.get(args.url, timeout=args.timeout, verify=args.ca or True) as response:
            response.raise_for_status()
            print(response.text)


if __name__ == "__main__":
    main()
