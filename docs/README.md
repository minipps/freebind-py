# freebind-py user guide

Use a routed IPv6 allocation as a pool of source addresses for Python sockets,
Requests, aiohttp, HTTPX, or curl_cffi. Inspired by and credited to
[blechschmidt's original Freebind](https://github.com/blechschmidt/freebind),
this library exposes explicit Python source policies; IPv4 is also supported.
See the [project README](../README.md#why-freebind) for the original motivation:
IPv6 source rotation and the difference between address and prefix rate limits.
The core has no runtime dependencies. Linux and Python 3.11 or newer are required
for socket operations; async integrations use asyncio.

## Install

Requires Python 3.11 or newer. Socket operations require Linux.

Install the core or the integration you need from PyPI:

```sh
python -m pip install freebind-py
python -m pip install 'freebind-py[requests]'
python -m pip install 'freebind-py[aiohttp]'
python -m pip install 'freebind-py[httpx]'
python -m pip install 'freebind-py[curl-cffi]'
python -m pip install 'freebind-py[all]'
```

The distribution is named `freebind-py`; import it as `freebind`.

| Extra | Allowed dependencies |
| --- | --- |
| `requests` | `requests>=2.34.2,<3`, `urllib3>=2.7,<3` |
| `aiohttp` | `aiohttp>=3.13.5,<4` |
| `httpx` | `httpx>=0.28.1,<0.29`, `httpcore>=1.0.9,<1.1`, `anyio>=4.10,<5` |
| `curl-cffi` | `curl_cffi>=0.16.3,<0.17` |
| `all` | All four integrations |

## Choose a source

A bare IP is a fixed, single-address source. By default, a CIDR is sampled in
`random` mode whenever a new socket is bound. `sticky` selects one address per
configured family when `Source` is created and keeps it for that policy. Sharing
a `Source` shares its sticky address.

```python
import socket
from freebind import Source, random_ip

# Vary the 16 subnet bits after /48; the remaining 64 bits are zero.
address = random_ip("2001:db8:100::/48", bits=16)
random_source = Source("2001:db8:100::/48", bits=16)
fixed_source = Source("2001:db8:100::17")
sticky_source = Source("2001:db8:100::/48", mode="sticky", bits=16)

both_families = Source(("198.51.100.0/24", "2001:db8:100::/64"))
assert both_families.families == frozenset({socket.AF_INET, socket.AF_INET6})
assert Source("2001:db8:100::17", strict=False).select(socket.AF_INET) is None
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

source = Source("2001:db8:100::17")
with new_socket(source, type=socket.SOCK_DGRAM) as udp:
    udp.settimeout(3)
    udp.sendto(b"hello", ("2001:db8:200::2", 9000))
    reply, peer = udp.recvfrom(65535)

# TCP connection helpers return caller-owned connected sockets.
sock = create_connection(("2001:db8:200::2", 9000), source, timeout=3)
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
    sock = await async_create_connection((host, port), Source("2001:db8:100::17"))
    try:
        return await asyncio.open_connection(sock=sock)
    except BaseException:
        sock.close()
        raise
```

On success, the asyncio writer owns the transferred socket.

See [examples/udp.py](../examples/udp.py) for connected and unconnected UDP, sync
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

source = Source("2001:db8:100::/48")
with requests.Session() as client:
    client.trust_env = False
    client.mount("http://", FreebindAdapter(source, fresh=True))
    client.mount("https://", FreebindAdapter(source, fresh=True))
    response = client.get("https://service.example/", timeout=5, verify="ca.pem")
```

For aiohttp, use the connector with the session’s native TLS options:

```python
import aiohttp
import ssl
from freebind import Source
from freebind.aiohttp import FreebindConnector

async def fetch(url):
    connector = FreebindConnector(Source("2001:db8:100::/48"), fresh=True)
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
transport = FreebindTransport(Source("2001:db8:100::/48"), fresh=True, verify=context)
with httpx.Client(transport=transport, trust_env=False) as client:
    response = client.get("https://service.example/", timeout=5)
```

Use `AsyncFreebindTransport` with `httpx.AsyncClient` for asyncio:

```python
import httpx
from freebind import Source
from freebind.httpx import AsyncFreebindTransport

async def fetch(url):
    transport = AsyncFreebindTransport(Source("2001:db8:100::/48"), fresh=True)
    async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
        response = await client.get(url, timeout=5)
        return response.text
```

curl_cffi uses sessions that preserve its native browser impersonation:

```python
from freebind import Source
from freebind.curl_cffi import FreebindSession, AsyncFreebindSession

source = Source("2001:db8:100::/64")  # Replace with your routed prefix.
with FreebindSession(source, impersonate="chrome", fresh=True) as client:
    response = client.get("https://service.example/", timeout=5)

async def fetch(url):
    async with AsyncFreebindSession(source, impersonate="chrome") as client:
        response = await client.get(url, timeout=5)
        return response.text
```

Install `freebind-py[curl-cffi]`; supported curl_cffi versions are `>=0.16.3,<0.17`.
The integration uses a private CFFI socket callback because curl_cffi does not
expose libcurl's socket callback through `Curl.setopt()`. It reuses `Source`
and `bind_socket()` while libcurl retains socket ownership, DNS, TLS, and HTTP
handling. Strict single-family policies restrict libcurl's destination family;
with `strict=False`, libcurl may use an unconfigured family without a source bind.

Sessions disable environment proxies by default and reject explicit proxies,
`interface`, custom curl handles, and raw socket/binding options that conflict
with Freebind. Keep TLS settings (`verify`/`cert`) and `impersonate` on the session
or request as usual. Buffered request binding errors retain their original
exception as a cause. Sync sessions create bound handles in each calling thread;
async sessions use asyncio. HTTP/2 and HTTP/3 remain managed by libcurl, but the
integration's network tests currently cover HTTP/1.1 over TLS only. WebSocket
support is not part of the tested integration contract.

See [examples/curl_cffi_client.py](../examples/curl_cffi_client.py) for a runnable
sync/async client. Python's process-wide `patch()` cannot intercept libcurl
sockets; use these sessions instead.

For mTLS, certificates, or custom trust roots, use the client’s usual options:
Requests `verify`/`cert`, aiohttp `ssl`, HTTPX transport `verify`/`cert`, and curl_cffi session `verify`/`cert`.
See [examples/requests_client.py](../examples/requests_client.py),
[examples/aiohttp_client.py](../examples/aiohttp_client.py), and
[examples/httpx_client.py](../examples/httpx_client.py) for runnable clients.

### Run the IPv6 examples

Replace the documentation prefix with your routed allocation and use an
IPv6-capable HTTP endpoint or TCP/UDP echo peer. These commands assume the
package and the corresponding optional extras are installed:

```sh
python examples/requests_client.py --source 2001:db8:100::/48 --url https://service.example/ --fresh
python examples/aiohttp_client.py --source 2001:db8:100::/48 --url https://service.example/ --fresh
python examples/httpx_client.py --source 2001:db8:100::/48 --url https://service.example/ --fresh --async
python examples/patch.py --source 2001:db8:100::/48 --host 2001:db8:200::2 --port 9000
python examples/udp.py --source 2001:db8:100::/48 --peer 2001:db8:200::2 --port 9000 --asyncio
```

Each HTTP command sends one request. Repeated runs sample new source addresses;
within a long-lived client, use `fresh=True` to sample for each request.
UDP keeps one source per socket, including when sending to multiple peers;
this library does not implement the original tool's `packetrand` packet rewriting.

### Pooling and fresh connections

A pooled connection keeps the source address it used when created. With
`fresh=True`, Requests, aiohttp, and HTTPX use a new TCP connection for each
transmitted request, including redirect and retry attempts. This does not mean a unique IP: fixed and sticky
sources keep selecting the same address, and random draws may repeat. A live
streaming response keeps its connection until consumed or closed. HTTPX fresh
mode requires HTTP/1.1 and rejects HTTP/2. curl_cffi fresh mode uses libcurl's
`FRESH_CONNECT` and `FORBID_REUSE`, forcing a new connection for each transfer
and closing it afterward. Redirect hops within one transfer may reuse a
connection; `fresh=True` does not promise a new source for every redirect hop.

## Optional process-wide patch

`patch()` temporarily hooks the existing `socket.socket` class. Its default
`entrypoint="connect"` prepares unbound TCP sockets on connect and UDP sockets
on their first send. `entrypoint="socket"` binds eligible sockets during
construction, so a later explicit `bind()` can fail. `socket_types` filters the
TCP/UDP types. Only one Freebind patch may be active at a time; restore its
`PatchHandle` or use it as a context manager:

```python
from freebind import Source, patch

with patch(Source("2001:db8:100::/48"), entrypoint="connect"):
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
See [examples/patch.py](../examples/patch.py).

## Routing and limits

Freebind permits binding a nonlocal source address; it does not make that source
reachable. The client needs a suitable local route for the source prefix (often
an AnyIP `local PREFIX dev lo` route), the peer needs a return route, and the
upstream network must carry the traffic. For a routed IPv6 allocation, add an
AnyIP route on the client, replacing this
documentation prefix with your actual allocation:

```sh
sudo ip -6 route add local 2001:db8:100::/48 dev lo
```

Configure the matching return route on your upstream router or peer. An IPv4
allocation uses `ip route add local PREFIX dev lo` instead. For an on-link
prefix, the router also needs to resolve the
source with ARP or IPv6 Neighbor Discovery (ND); a local route alone does not
configure upstream neighbor discovery. Readiness depends on
your Linux routes and network hardware. This library changes no routes, sysctls,
or firewall rules.

NAT can change the source observed by a remote peer. A proxy makes the proxy’s
address visible instead, which is why proxies are unsupported and examples turn
off environment proxy settings. These rules apply independently of source
selection and connection freshness.

## Troubleshooting

- **Binding succeeds but requests time out:** check the local route, the peer's
  return route, and upstream routing for the chosen source prefix. Freebind does
  not make a nonlocal address reachable.
- **`FamilyMismatchError`:** configure a source for the destination's address
  family, or use `strict=False` if wildcard fallback is appropriate.
- **Permission errors with `interface=`:** check the permissions for
  `SO_BINDTODEVICE` in your environment. Route changes may also require privileges.
- **Proxy configuration errors:** disable environment proxies with
  `trust_env=False` and remove explicit proxy settings.
- **Repeated source addresses:** fixed and sticky policies intentionally reuse
  an address; random selection can repeat, and pooled connections keep their
  original source. Use `fresh=True` on HTTP integrations when each request needs
  a new connection.

See [contributing and releases](CONTRIBUTING.md) to work on the package.
