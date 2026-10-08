"""Send one HTTP(S) request through the sync or async HTTPX Freebind transport."""

import argparse
import asyncio
import ssl

import httpx

from freebind import Source
from freebind.httpx import AsyncFreebindTransport, FreebindTransport


def _source(args):
    return Source(args.source, mode="sticky" if args.sticky else "random")


def _request(args):
    transport = FreebindTransport(
        _source(args),
        fresh=args.fresh,
        verify=ssl.create_default_context(cafile=args.ca) if args.ca else True,
    )
    with httpx.Client(transport=transport, trust_env=False) as client:
        response = client.get(args.url, timeout=args.timeout)
        response.raise_for_status()
        print(response.text)


async def _async_request(args):
    transport = AsyncFreebindTransport(
        _source(args),
        fresh=args.fresh,
        verify=ssl.create_default_context(cafile=args.ca) if args.ca else True,
    )
    async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
        response = await client.get(args.url, timeout=args.timeout)
        response.raise_for_status()
        print(response.text)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="source IP or routed CIDR, e.g. 2001:db8:100::/48 (replace with your allocation)")
    parser.add_argument("--url", required=True)
    parser.add_argument("--async", dest="use_async", action="store_true", help="use HTTPX's asyncio client")
    parser.add_argument("--fresh", action="store_true", help="close each connection after its response")
    parser.add_argument("--sticky", action="store_true", help="reuse one selected source address")
    parser.add_argument("--ca", help="CA bundle used to verify HTTPS")
    parser.add_argument("--timeout", type=float, default=10)
    args = parser.parse_args()
    if args.use_async:
        asyncio.run(_async_request(args))
    else:
        _request(args)


if __name__ == "__main__":
    main()
