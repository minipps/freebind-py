# freebind-py

Bind Linux IPv4/IPv6 TCP and UDP sockets to a chosen source address, including a
nonlocal address. The core uses Python's standard library; Requests, aiohttp,
and HTTPX integrations are optional. Import the package anywhere, but Freebind
socket operations require Linux. The compatibility workflow targets Python
3.11–3.14. Async support targets asyncio; Trio is not supported.

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

with new_socket(Source("127.0.0.1"), type=socket.SOCK_DGRAM) as sock:
    print(sock.getsockname())
```

For a routed prefix, create a policy and pass it to a socket helper or HTTP
integration. Replace the documentation address and URL with your own:

```python
import requests
from freebind import Source
from freebind.requests import FreebindAdapter

source = Source("198.51.100.17")
with requests.Session() as client:
    client.trust_env = False
    client.mount("https://", FreebindAdapter(source))
    response = client.get("https://service.example/", timeout=5)
    response.raise_for_status()
```

Install the `requests` extra to use this example. aiohttp and HTTPX integrations
also support asyncio; HTTPX provides both sync and async transports.

## Source policies and connections

- An IP selects a fixed source address.
- A CIDR selects a random address for each new socket.
- `Source(prefix, mode="sticky")` selects one address per configured family and
  reuses it for that policy.
- `bits=` controls how many leading host bits are randomized.
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

## License

GPL-3.0-only. The original C Freebind project is also GPL-3.0. The AGPL-3.0
freebind.js project informed behavior only; no JavaScript implementation code
was copied into this package.
