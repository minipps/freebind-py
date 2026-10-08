# freebind-py

Use the IPv6 addresses in a prefix routed to your Linux machine, without
assigning every address to a network interface. freebind-py brings the idea of
[the original Freebind by blechschmidt](https://github.com/blechschmidt/freebind)
to Python sockets, Requests, aiohttp, and HTTPX. Credit for the original tool
and its IPv6 source-rotation approach belongs to that project.

## Why Freebind?

An IPv6 allocation gives one host many possible source addresses. Freebind
lets each new socket bind to a randomly selected address in that allocation.
This can bypass IPv6 rate limits keyed only by source address (`/128`). If a
service limits whole `/64` prefixes, rotating addresses inside one `/64` will
not help; a routed `/48`, for example, lets you select addresses across many
`/64`s. This is one machine using many IPs, not one fixed IP changing prefixes.
Account-based limits and limits covering your whole allocation still apply.

The core uses Python's standard library and supports IPv4 as well as IPv6,
TCP and UDP. HTTP integrations are optional. Socket operations require Linux;
Python 3.11–3.14 is covered by CI. Async support uses asyncio.

## Install

Requires Python 3.11 or newer. Socket operations require Linux.

Install the core or the integration you need from PyPI:

```sh
python -m pip install freebind-py
python -m pip install 'freebind-py[requests]'
python -m pip install 'freebind-py[aiohttp]'
python -m pip install 'freebind-py[httpx]'
python -m pip install 'freebind-py[all]'
```

The distribution is named `freebind-py`; import it as `freebind`.

## Quick start

Use a source address your network routes to this machine. This example uses
loopback so it can run locally without configuring nonlocal routes:

```python
import socket
from freebind import Source, new_socket

with new_socket(Source("::1"), type=socket.SOCK_DGRAM) as sock:
    print(sock.getsockname())
```

For source rotation, use an IPv6 prefix routed to your machine and a destination
with an IPv6 address. Replace `2001:db8:100::/48` (a documentation-only prefix)
and the URL below with your own. On Linux, configure the local AnyIP route for
your actual allocation first:

```sh
sudo ip -6 route add local 2001:db8:100::/48 dev lo
```

This route does not obtain an allocation or configure your upstream router.
Your provider must route the prefix back to this machine; on-link allocations
also need working Neighbor Discovery. Then select a new source per connection:

```python
import requests
from freebind import Source
from freebind.requests import FreebindAdapter

source = Source("2001:db8:100::/48")
with requests.Session() as client:
    client.trust_env = False
    client.mount("https://", FreebindAdapter(source, fresh=True))
    for _ in range(3):
        response = client.get("https://service.example/", timeout=5)
        response.raise_for_status()
        print(response.text)
```

Install the `requests` extra to use this example. aiohttp and HTTPX integrations
also support asyncio; HTTPX provides both sync and async transports.

## Source policies and connections

- An IP selects a fixed source address.
- A CIDR selects a random address for each new socket.
- `Source(prefix, mode="sticky")` selects one address per configured family and
  reuses it for that policy.
- `bits=` controls how many leading host bits are randomized. For a `/48`,
  `bits=16` varies the next 16 bits, selecting among its `/64` prefixes.
- HTTP clients pool connections by default. Pass `fresh=True` to an adapter,
  connector, or transport to create a new connection per request.

Freebind allows binding nonlocal addresses; it does not configure routing.
The machine needs a local route for the source prefix, the peer needs a return
route, and the upstream network must carry the traffic. A successful bind alone
does not establish reachability. Explicit HTTP proxies are unsupported; disable
environment proxies with `trust_env=False`.

## Documentation

The [user guide](https://github.com/minipps/freebind-py/blob/main/docs/README.md)
covers IPv4/IPv6 source selection, TCP/UDP sockets, asyncio, Requests, aiohttp,
HTTPX, TLS, pooling, process-wide patching, and troubleshooting.
Documentation and runnable examples are also included in the source distribution
under `docs/` and `examples/`.

See [contributing and releases](https://github.com/minipps/freebind-py/blob/main/docs/CONTRIBUTING.md)
for local setup, linting, tests, and publishing.

See the [changelog](https://github.com/minipps/freebind-py/blob/main/CHANGELOG.md)
for release notes.

## License

GPL-3.0-only. The [original C Freebind](https://github.com/blechschmidt/freebind)
project is also GPL-3.0. The AGPL-3.0
freebind.js project informed behavior only; no JavaScript implementation code
was copied into this package.
