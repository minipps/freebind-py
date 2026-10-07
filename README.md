# freebind-py

Bind Linux IPv4/IPv6 TCP and UDP sockets to a chosen source address, including a
nonlocal address. The core uses Python's standard library; Requests, aiohttp,
and HTTPX integrations are optional. Import the package anywhere, but Freebind
socket operations require Linux. The compatibility workflow targets Python
3.11–3.14. Async support targets asyncio; Trio is not supported.

## Install

From a checkout, install the core or an integration extra:

```sh
python -m pip install .
python -m pip install '.[requests]'
python -m pip install '.[aiohttp]'
python -m pip install '.[httpx]'
python -m pip install '.[all]'
```

The distribution metadata identifies this project as `freebind-py` version
`0.1.0`; these commands install the checkout and do not imply a PyPI release.

| Extra | Allowed dependencies |
| --- | --- |
| `requests` | `requests>=2.34.2,<3`, `urllib3>=2.7,<3` |
| `aiohttp` | `aiohttp>=3.13.5,<4` |
| `httpx` | `httpx>=0.28.1,<0.29`, `httpcore>=1.0.9,<1.1`, `anyio>=4.10,<5` |
| `all` | All three integrations |

## Choose a source

A bare IP is a fixed, single-address source. By default, a CIDR is sampled in
`random` mode whenever a new socket is bound. `sticky` selects one address per
configured family when `Source` is created and keeps it for that policy. Sharing
a `Source` shares its sticky address.

```python
import socket
from freebind import Source, random_ip

# Randomize the first 4 host bits; the remaining host bits are zero.
address = random_ip("198.51.100.0/24", bits=4)
random_source = Source("198.51.100.0/24", bits=4)
fixed_source = Source("198.51.100.17")
sticky_source = Source("198.51.100.0/24", mode="sticky", bits=4)

both_families = Source(("198.51.100.0/24", "2001:db8:100::/64"))
assert both_families.families == frozenset({socket.AF_INET, socket.AF_INET6})
assert Source("198.51.100.17", strict=False).select(socket.AF_INET6) is None
```

The documentation-only addresses above will not route on a real network. Replace
them with a prefix that your network routes for you. Pass `interface="eth0"` to
`Source(prefixes, ...)` to request Linux `SO_BINDTODEVICE`; kernel permissions
and errors apply. `bits=None` (the default) randomizes all host bits; `bits=0` selects the network base. Prefixes are
normalized, and repeated prefixes count as repeated choices. Random selection
does not guarantee unique addresses.

`Source.select(family)` returns the selected address. The default `strict=True`
raises `FamilyMismatchError` if no prefix matches that family. With
`strict=False`, `select()` returns `None` for an absent family; socket helpers
bind the wildcard address in that case. For connection helpers, fallback to an
unconfigured family is allowed only when DNS has no matching destination
family. A bind or connection failure never triggers fallback. Mixed-family
policies require `family=` when calling `new_socket()`.

Scoped IPv6, link-local, and IPv4-mapped IPv6 source prefixes are rejected.

## Sockets

`enable_freebind(sock)` sets Linux's Freebind option. `bind_socket(sock, source,
port=0)` applies a policy to an unbound caller-owned socket. `new_socket()`
creates a bound, unconnected TCP or UDP socket; it returns a caller-owned socket
that should be closed normally.

```python
from freebind import Source, create_connection, new_socket
import socket

source = Source("198.51.100.17")
with new_socket(source, type=socket.SOCK_DGRAM) as udp:
    udp.settimeout(3)
    udp.sendto(b"hello", ("peer.example", 9000))
    reply, peer = udp.recvfrom(65535)

# TCP connection helpers return caller-owned connected sockets.
sock = create_connection(("peer.example", 9000), source, timeout=3)
sock.close()
```

For asyncio, await
`async_create_connection((host, port), source, timeout=...)`; it returns a
connected nonblocking socket that you can transfer to
`asyncio.open_connection(sock=sock)` or another asyncio transport. Close the
socket yourself if you do not transfer it. Helpers close sockets they own when
setup or connection fails. `create_connection()` and
`async_create_connection()` accept `timeout` and `socket_options` tuples; async
resolution does not block the event loop. To hand an async socket to asyncio:

```python
import asyncio
from freebind import Source, async_create_connection

async def open_stream(host, port):
    sock = await async_create_connection((host, port), Source("198.51.100.17"))
    try:
        return await asyncio.open_connection(sock=sock)
    except BaseException:
        sock.close()
        raise
```

On success, the asyncio writer owns the transferred socket.

See [examples/udp.py](examples/udp.py) for connected and unconnected UDP, sync
and asyncio modes. The example expects a reachable UDP echo service.

## HTTP clients

The adapters preserve each client’s TLS, SNI, timeout, streaming, retry, and
redirect behavior. Normal pooling is the default. Set `trust_env=False` so
ambient proxy variables cannot reroute example requests. Explicit proxies are
not supported by these Freebind integrations.

```python
import requests
from freebind import Source
from freebind.requests import FreebindAdapter

source = Source("198.51.100.17")
with requests.Session() as client:
    client.trust_env = False
    client.mount("http://", FreebindAdapter(source))
    client.mount("https://", FreebindAdapter(source))
    response = client.get("https://service.example/", timeout=5, verify="ca.pem")
```

For aiohttp, use the connector with the session’s native TLS options:

```python
import aiohttp
import ssl
from freebind import Source
from freebind.aiohttp import FreebindConnector

async def fetch(url):
    connector = FreebindConnector(Source("198.51.100.17"))
    context = ssl.create_default_context(cafile="ca.pem")
    async with aiohttp.ClientSession(
        connector=connector, trust_env=False
    ) as client:
        async with client.get(url, ssl=context, timeout=5) as response:
            return await response.text()
```

HTTPX uses a transport; pass native TLS settings to it and disable environment
proxy discovery on the client:

```python
import httpx
import ssl
from freebind import Source
from freebind.httpx import FreebindTransport

context = ssl.create_default_context(cafile="ca.pem")
transport = FreebindTransport(Source("198.51.100.17"), verify=context)
with httpx.Client(transport=transport, trust_env=False) as client:
    response = client.get("https://service.example/", timeout=5)
```

Use `AsyncFreebindTransport` with `httpx.AsyncClient` for asyncio:

```python
import httpx
from freebind import Source
from freebind.httpx import AsyncFreebindTransport

async def fetch(url):
    transport = AsyncFreebindTransport(Source("198.51.100.17"))
    async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
        response = await client.get(url, timeout=5)
        return response.text
```

For mTLS, certificates, or custom trust roots, use the client’s usual options:
Requests `verify`/`cert`, aiohttp `ssl`, and HTTPX transport `verify`/`cert`.
See [examples/requests_client.py](examples/requests_client.py),
[examples/aiohttp_client.py](examples/aiohttp_client.py), and
[examples/httpx_client.py](examples/httpx_client.py) for runnable clients.

### Pooling and fresh connections

A pooled connection keeps the source address it used when created. With
`fresh=True`, each transmitted request uses a new TCP connection, including
redirect and retry attempts. This does not mean a unique IP: fixed and sticky
sources keep selecting the same address, and random draws may repeat. A live
streaming response keeps its connection until consumed or closed. HTTPX fresh
mode requires HTTP/1.1 and rejects HTTP/2.

## Optional process-wide patch

`patch()` temporarily hooks the existing `socket.socket` class. Its default
`entrypoint="connect"` prepares unbound TCP sockets on connect and UDP sockets
on their first send. `entrypoint="socket"` binds eligible sockets during
construction, so a later explicit `bind()` can fail. `socket_types` filters the
TCP/UDP types. Only one Freebind patch may be active at a time; restore its
`PatchHandle` or use it as a context manager:

```python
from freebind import Source, patch

with patch(Source("198.51.100.17"), entrypoint="connect"):
    pass
```

The patch is process-wide and can conflict with other code that replaces the
same socket methods; `restore()` refuses to overwrite methods changed while the
patch is active. Socket-construction timing affects newly created sockets, not
adopted, accepted, or duplicated descriptors. It patches the existing class, so
aliases to `socket.socket` see the hooks; saved references to original methods do
not.
Library helpers and adapters preserve their explicit source policy and bypass
the hooks to avoid double binding.

This is Python-level interception, not `LD_PRELOAD`: it does not cover native
extensions, direct `socket._socket` calls, cached original methods, or native
event-loop socket creation such as uvloop. It provides no subprocess
inheritance guarantee. Applications needing libc-level interception can use
the [original Freebind launcher](https://github.com/blechschmidt/freebind).

`patch_from_env()` reads settings only when called; importing `freebind` never
activates a patch. It requires `FREEBIND_RANDOM` (comma/space-separated IPs or
CIDRs), optionally reads `FREEBIND_IFACE`, accepts `FREEBIND_TYPE_FILTER=STREAM`
or `DGRAM`, and accepts `FREEBIND_ENTRYPOINT=socket` or `connect`. The
entrypoint defaults to `socket` for compatibility with the C launcher.
See [examples/patch.py](examples/patch.py).

## Routing and limits

Freebind permits binding a nonlocal source address; it does not make that source
reachable. The client needs a suitable local route for the source prefix (often
an AnyIP `local PREFIX dev lo` route), the peer needs a return route, and the
upstream network must carry the traffic. For example, add `ip route add local PREFIX dev lo` on the client (use
`ip -6 route add local PREFIX dev lo` for IPv6) and configure the matching return
route on the peer. For an on-link prefix, the router also needs to resolve the
source with ARP or IPv6 Neighbor Discovery (ND); a local route alone does not
configure upstream neighbor discovery. Readiness depends on
your Linux routes and network hardware. This library changes no routes, sysctls,
or firewall rules.

NAT can change the source observed by a remote peer. A proxy makes the proxy’s
address visible instead, which is why proxies are unsupported and examples turn
off environment proxy settings. These rules apply independently of source
selection and connection freshness.

## Checks and compatibility

Run unit tests with:

```sh
.venv/bin/python -m unittest discover -s tests
```

Outside the namespace harness, the eight real-network tests are intentionally
skipped. To run the eight parity checks with real IPv4/IPv6 source and return
traffic, use:

```sh
.venv/bin/python tests/network_harness.py -- .venv/bin/python -m unittest discover -s tests -p test_network.py -v
```

The [CI workflow](.github/workflows/tests.yml) configures unit jobs for CPython
3.11–3.14 with minimum and newest allowed optional dependencies, namespace jobs
for both dependency sets on CPython 3.12, and an ARM64 CPython 3.14 smoke job
with newest allowed dependencies. This describes configured coverage; consult
the workflow run status for results for a particular revision.

## License

GPL-3.0-only. The original C Freebind project is also GPL-3.0. The AGPL-3.0
freebind.js project informed behavior only; no JavaScript implementation code
was copied into this package.
