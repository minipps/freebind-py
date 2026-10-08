"""Send one HTTP(S) request through the aiohttp Freebind connector."""

import argparse
import asyncio
import ssl

import aiohttp

from freebind import Source
from freebind.aiohttp import FreebindConnector


async def _request(args):
    source = Source(args.source, mode="sticky" if args.sticky else "random")
    connector = FreebindConnector(source, fresh=args.fresh)
    ssl_context = ssl.create_default_context(cafile=args.ca) if args.ca else None
    async with aiohttp.ClientSession(connector=connector, trust_env=False) as client:
        async with client.get(
            args.url, timeout=args.timeout, ssl=ssl_context
        ) as response:
            response.raise_for_status()
            print(await response.text())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="source IP or routed CIDR, e.g. 2001:db8:100::/48 (replace with your allocation)")
    parser.add_argument("--url", required=True)
    parser.add_argument("--fresh", action="store_true", help="close each connection after its response")
    parser.add_argument("--sticky", action="store_true", help="reuse one selected source address")
    parser.add_argument("--ca", help="CA bundle used to verify HTTPS")
    parser.add_argument("--timeout", type=float, default=10)
    asyncio.run(_request(parser.parse_args()))


if __name__ == "__main__":
    main()
