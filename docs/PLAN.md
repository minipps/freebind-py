# Freebind Python library implementation plan

Agreed on 2026-10-07. Task IDs and implementation progress live in
[TASKS.md](TASKS.md); concurrent-agent rules live in [README.md](README.md).

## 1. Summary and scope

Implement **freebind-py**, imported as `freebind`, using a pure-Python,
standard-library-only core. Provide optional Requests, aiohttp, and HTTPX
integrations, plus an explicitly enabled monkeypatch.

The agreed first release targets GPL-3.0-only, Linux, Python 3.11+, and asyncio.
It supports IPv4/IPv6 TCP and UDP, fixed addresses, multiple random source
prefixes, sticky addresses, interface selection, both patch timings, and opt-in
environment configuration. HTTP integrations preserve normal pooling by default
and provide an optional fresh connection for each request.

Exclude Trio, supported proxy transports, scoped/link-local sources,
IPv4-mapped IPv6 source configurations, native event-loop interception
guarantees, and the separate NFQUEUE packet-rewriting daemon. Publication is not
part of the implementation work; build reviewable release artifacts.

## 2. Research findings and architecture decision

### Upstream C Freebind

Research baseline: commit
[`70f88c3442dee3b58af67f9262d4a7e8973eb66a`](https://github.com/blechschmidt/freebind/commit/70f88c3442dee3b58af67f9262d4a7e8973eb66a).

The C project interposes libc `socket()` and `connect()` using `LD_PRELOAD`. It
enables Freebind, optionally chooses a source address from a matching-family
prefix, binds an ephemeral port, and optionally applies `SO_BINDTODEVICE`.
Configuration includes `FREEBIND_RANDOM`, `FREEBIND_TYPE_FILTER`,
`FREEBIND_ENTRYPOINT`, and `FREEBIND_IFACE`; the default bind timing is socket
creation. Prefixes are selected uniformly within the socket's address family.
Some configuration and socket errors are logged or ignored rather than stopping
the outgoing operation. The Python version deliberately validates inputs and
propagates setup failures.

Sources: [socket interposer](https://github.com/blechschmidt/freebind/blob/70f88c3442dee3b58af67f9262d4a7e8973eb66a/src/freebind.c),
[CIDR implementation](https://github.com/blechschmidt/freebind/blob/70f88c3442dee3b58af67f9262d4a7e8973eb66a/src/cidr.h),
[launcher](https://github.com/blechschmidt/freebind/blob/70f88c3442dee3b58af67f9262d4a7e8973eb66a/src/preloader.c).

### JavaScript implementation

Research baseline: commit
[`ab72e94c49e6511d349cc51e8b9a5b6802512114`](https://github.com/imputnet/freebind.js/commit/ab72e94c49e6511d349cc51e8b9a5b6802512114).

freebind.js recreates the behavior using socket syscalls instead of calling the
C project. Undici connectors provide fixed, random-per-connection, and
random-sticky sources. Its address generator has byte-alignment limitations,
and its socket implementation currently provides TCP. The Python library should
reproduce the useful behavior without reproducing these limitations or relying
on incidental upstream bugs.

Sources: [socket implementation](https://github.com/imputnet/freebind.js/blob/ab72e94c49e6511d349cc51e8b9a5b6802512114/socket.js),
[connectors](https://github.com/imputnet/freebind.js/blob/ab72e94c49e6511d349cc51e8b9a5b6802512114/dispatcher.js),
[address generator](https://github.com/imputnet/freebind.js/blob/ab72e94c49e6511d349cc51e8b9a5b6802512114/ip.js).

### Why the core does not need CFFI

Python exposes the required socket operations. Python 3.14 added the named
`IP_FREEBIND` constant; older supported versions can use its Linux value. Linux
also defines `IPV6_FREEBIND`. `ipaddress` handles parsing and arbitrary prefix
lengths, and `secrets` supplies randomness.

Sources: [Python sockets](https://docs.python.org/3/library/socket.html),
[ipaddress](https://docs.python.org/3/library/ipaddress.html),
[secrets](https://docs.python.org/3/library/secrets.html),
[IPv4 constants](https://github.com/torvalds/linux/blob/master/include/uapi/linux/in.h),
[IPv6 constants](https://github.com/torvalds/linux/blob/master/include/uapi/linux/in6.h).

| Approach | Assessment | Decision |
| --- | --- | --- |
| Python socket/ipaddress/secrets | Direct socket setup and binding; no compiler or runtime dependency | Use for core |
| CFFI/ctypes around upstream | Interposer with constructor-loaded global state, not a stable application API; harder packaging and ownership | Exclude |
| Original launcher around Python | Useful for unchanged programs and libc-using extensions; external binary/library required | Document alternative |
| Python monkeypatch | Covers supported Python paths with process-wide effects | Include, opt-in |
| Port NFQUEUE packetrand | Separate privileged packet-processing subsystem | Exclude |

Loading a shared library into an already running interpreter is not equivalent
to arranging `LD_PRELOAD` before startup. Applications needing libc-level
interception should use the original launcher.
[Dynamic loader documentation](https://man7.org/linux/man-pages/man8/ld.so.8.html).

### Networking prerequisites and evidence limits

Freebind permits a nonlocal source bind; it does not establish routing.
Communication still requires a routed prefix, suitable local/return routes, and
an upstream network that carries the selected source. The library must not
modify routes, sysctls, or firewall rules automatically. Documentation should
explain AnyIP setup, NAT/proxy effects on observed addresses, and why an on-link
prefix can need additional neighbor-discovery configuration.

Sources: [upstream setup and packet-rewriting distinction](https://github.com/blechschmidt/freebind#setup),
[kernel option semantics](https://man7.org/linux/man-pages/man2/IP_FREEBIND.2const.html),
[kernel IP sysctls](https://www.kernel.org/doc/html/latest/networking/ip-sysctl.html).

Read-only arithmetic probes passed partial randomization, non-byte-aligned
prefixes, singleton networks, and IPv6 `/0`. Ordinary socket construction
returned `EPERM` in the original read-only research environment. Subsequent
implementation validation outside that sandbox passed real IPv4/IPv6 namespace
parity locally and in hosted CI with all client capabilities dropped. Current
acceptance evidence is recorded in [TASKS.md](TASKS.md).

## 3. Public API and behavior contract

### Core interfaces

The following is API notation, not executable Python syntax:

```text
Source(prefixes: str | Sequence[str], *, mode="random", bits=None,
       strict=True, interface=None)
Source.select(family) -> str | None

random_ip(cidr, *, bits=None) -> str
enable_freebind(sock) -> None
bind_socket(sock, source, *, port=0) -> str | None

new_socket(source, *, family=None, type=socket.SOCK_STREAM, port=0)
    -> socket.socket
create_connection(address, source, *, timeout=None, socket_options=())
    -> socket.socket
async_create_connection(address, source, *, timeout=None, socket_options=())
    -> awaitable[socket.socket]

patch(source, *, entrypoint="connect",
      socket_types=(socket.SOCK_STREAM, socket.SOCK_DGRAM)) -> PatchHandle
PatchHandle.restore() -> None
PatchHandle supports with-statements
patch_from_env() -> PatchHandle

FamilyMismatchError(OSError), errno EAFNOSUPPORT
```

`Source.select()` selects an address of the requested socket family. It raises
`FamilyMismatchError` when that family is absent and `strict=True`, or returns
`None` when it is absent and `strict=False`.

`new_socket()` returns a bound, unconnected socket. With `SOCK_DGRAM`, it can use
ordinary socket methods or asyncio datagram APIs. Infer its family only for a
single-family policy; mixed policies require an explicit family.

`async_create_connection()` returns a connected, nonblocking socket. Callers can
transfer it to `asyncio.open_connection(sock=...)` or an existing transport.
Returned sockets belong to callers; creation helpers close them on failure.

### Address selection

- Parse with `ipaddress`; normalize CIDRs containing host bits, matching C.
- Bare IPs become singleton networks, giving fixed behavior without a separate
  source mode.
- `mode` accepts only `"random"` and `"sticky"`.
- Choose a prefix uniformly among matching-family entries, then draw randomized
  bits with `secrets`. Preserve repeated entries as repeated selection entries.
- Support every prefix length without enumerating addresses.
- `bits=k` randomizes the first `k` host bits after the prefix and zeros the rest:

  ```text
  host_bits = network.max_prefixlen - network.prefixlen
  value = int(network.network_address) | (random_k_bits << (host_bits - k))
  address = type(network.network_address)(value)
  ```

- `bits=None` uses all available host bits. Validate integer bounds against every
  configured prefix; reject negative or excessive counts and invalid inputs.
- Sticky mode selects one address per configured family at construction. Sharing
  a `Source` intentionally shares its sticky identity and requires no lazy cache.
- Random mode draws for each new socket attempt. Pooled connections keep their
  address. Draws do not guarantee uniqueness.
- Sample the full configured address space, as upstream does. Caller-supplied
  ranges must be suitable for routing; the library is not an address allocator.
- Reject explicit scoped/link-local and IPv4-mapped IPv6 source configurations.

### Socket setup, ownership, and failures

- IPv4 uses `IPPROTO_IP` and `getattr(socket, "IP_FREEBIND", 15)`.
- IPv6 uses `IPPROTO_IPV6` and `getattr(socket, "IPV6_FREEBIND", 78)`. Fall back
  to the upstream IPv4-level option only on an unsupported-option error, never on
  permission errors. Linux stores both in the same Freebind state.
  [IPv6 kernel implementation](https://github.com/torvalds/linux/blob/master/net/ipv6/ipv6_sockglue.c).
- Apply caller options and interface selection before source binding; enable
  Freebind before `bind()`. Do not permit supplied options to disable the required
  Freebind setting accidentally.
- Validate interface names without truncation or embedded NULs; use
  `SO_BINDTODEVICE` and propagate its errors. An explicitly selected interface
  also applies when non-strict mode allows ordinary source selection.
- Preserve kernel errno. Close owned sockets on setup, connection, timeout, and
  cancellation failures. Never silently send from an ordinary source after a
  requested Freebind setup fails.
- `bind_socket()` requires an unbound caller-owned socket. Ownership stays with
  the caller even on failure; do not claim to roll back a completed bind.
- Non-Linux Freebind operations raise `NotImplementedError`. Package import and
  address generation remain usable.

### DNS, fallback, and timeouts

- Resolve destination candidates; use configured source families whenever
  matching candidates exist and attempt them in resolver order.
- `strict=True` rejects a destination with no matching candidate using
  `FamilyMismatchError`. Preserve actual DNS failures as DNS errors.
- `strict=False` permits ordinary source selection only when resolution yields
  no matching-family candidate. A matching-family bind or connection failure
  does not permit fallback to an unconfigured family.
- Return a normal socket on success. Native client integrations keep their usual
  error types and retain the underlying exception cause.
- Use one connection-timeout budget across candidates. Async DNS is included.
  Synchronous OS DNS cannot be interrupted, but elapsed DNS time reduces the
  remaining budget. Successful sockets retain the caller's requested timeout;
  async sockets remain nonblocking.
- Core helpers attempt candidates sequentially. aiohttp retains native Happy
  Eyeballs after family filtering.

### Monkeypatch contract

Patch methods on the existing Python `socket.socket` class. Preserve class
identity and imported constructor aliases; do not replace it with a factory.

- Default `entrypoint="connect"` prepares an unbound socket immediately before
  `connect()`, `connect_ex()`, or the first UDP `sendto()`/`sendmsg()`.
- Preserve explicit bindings and connected sockets. Check local address as well
  as port so an address-only bind is not mistaken for an unbound socket. Choose
  a source once, including across UDP destination changes.
- Preserve `connect_ex()`'s errno-return contract for setup errors and its normal
  nonblocking results.
- `entrypoint="socket"` prepares eligible sockets during construction. Skip
  construction around an existing descriptor. Subsequent explicit binds may
  conflict with the already established binding, as in C's default mode.
- Match actual TCP/UDP socket types, accounting for creation flags. Do not
  intercept Unix/raw sockets. Accepted, duplicated, and adopted bound sockets
  must not be rebound.
- Permit one active patch per interpreter; reject nested installations.
  Synchronize installation/restoration. Restoration is idempotent, restores
  inherited attributes correctly, and refuses to overwrite a subsequent patch.
- A context manager controls lifetime, not thread/task isolation. Eligible
  outgoing operations during that lifetime are process-wide; existing unbound
  Python socket objects can also be affected by outgoing-operation hooks.
- Explicit helpers/adapters under an active patch must not double bind or adopt
  the global source instead of their explicit source. Use saved original socket
  operations in library-controlled setup paths where needed.
- Document bypasses: native extensions, direct `_socket` calls, cached original
  methods, and native event-loop socket creation. No Python-patch subprocess
  inheritance guarantee.
- Callers needing Freebind for an explicit nonlocal bind should call
  `enable_freebind()` before binding, or use socket-construction timing.

### Explicit environment helper

`patch_from_env()` reads configuration only when called:

| Variable | Behavior |
| --- | --- |
| `FREEBIND_RANDOM` | Required nonempty IP/CIDR list; split commas/whitespace and validate every token |
| `FREEBIND_TYPE_FILTER` | Absent means TCP+UDP; accept `STREAM` or `DGRAM` case-insensitively |
| `FREEBIND_ENTRYPOINT` | Absent means `socket` for C compatibility; accept `socket` or `connect` case-insensitively |
| `FREEBIND_IFACE` | Optional interface name, passed to the source policy |

Environment configuration uses random mode and full host-bit randomization.
Importing the package never reads these settings or installs a patch. Unlike C,
the helper rejects malformed prefixes and missing source configuration; the
standalone `enable_freebind()` covers option-only use.

## 4. HTTP integrations

### Public interfaces

```text
freebind.requests.FreebindAdapter(source, *, fresh=False, ...)
freebind.aiohttp.FreebindConnector(source, *, fresh=False, ...)
freebind.httpx.FreebindTransport(source, *, fresh=False, ...)
freebind.httpx.AsyncFreebindTransport(source, *, fresh=False, ...)
```

Retain native TLS verification, SNI, streaming, redirects, timeouts, and ordinary
pool limits. Accept native client options that do not conflict with Freebind;
reject conflicting local-address/socket-factory settings. Each adapter owns its
policy and pool; do not change client-library globals.

### Requests

Use urllib3 HTTP/HTTPS connection subclasses that obtain sockets from the core.
Keep policy configuration out of pool-key arguments; store it on the manager and
inject it after pool-key construction. Copy pool-class mappings per manager.
Preserve Requests/urllib3 connection-error wrapping, auditing, and TLS behavior.
Reject proxy use rather than allowing an ordinary proxy manager to bypass setup.

Sources: [Requests adapter](https://github.com/psf/requests/blob/v2.32.5/src/requests/adapters.py),
[urllib3 connection implementation](https://github.com/urllib3/urllib3/blob/2.2.0/src/urllib3/connection.py),
[urllib3 connection reference](https://urllib3.readthedocs.io/en/stable/reference/urllib3.connection.html).

### aiohttp

Use the documented `socket_factory` hook for source-bound sockets. Add the small
connector override needed to filter resolved address families before Happy
Eyeballs, including numeric destinations and cached resolver results. Numeric
results must be classified from the literal rather than trusting an `AF_UNSPEC`
placeholder. Retain native DNS caching, tracing, and losing-socket cleanup.
Reject proxies before connecting or returning a pooled connection. Reject
conflicting `local_addr` and socket-factory options.

The hook was introduced in aiohttp 3.12. Sources:
[socket factory](https://docs.aiohttp.org/en/stable/client_advanced.html#custom-socket-creation),
[introduction](https://docs.aiohttp.org/en/v3.12.0/changes.html),
[connector implementation](https://docs.aiohttp.org/en/v3.12.0/_modules/aiohttp/connector.html).

### HTTPX synchronous and asynchronous transports

Provide HTTPcore network backends. Default sync and async backends apply socket
options after connecting, too late for Freebind source binding. Reuse the
existing HTTPcore stream/TLS implementations rather than copying or rewriting
them. Subclass HTTPX's transports, retain native initialization and request
conversion, and replace `_pool._network_backend` before first use.

Isolate private transport/stream compatibility points in the HTTPX integration
module. The async backend supports asyncio; wrap a connected nonblocking socket
with `anyio.abc.SocketStream.from_socket()` and HTTPcore's existing AnyIO stream.
Transfer ownership exactly once and close the socket if wrapping is cancelled
or fails. AnyIO added this public wrapping API in 4.10.

Reject proxy, Unix-domain-socket, and conflicting `local_address` configuration.
Retain normal HTTP/2 support when `fresh=False` and native HTTPX requirements
are satisfied; do not add an unconditional HTTP/2 dependency.

Sources: [sync backend](https://github.com/encode/httpcore/blob/1.0.9/httpcore/_backends/sync.py),
[async backend](https://github.com/encode/httpcore/blob/1.0.9/httpcore/_backends/anyio.py),
[public backend interface](https://www.encode.io/httpcore/network-backends/),
[HTTPX transport implementation](https://github.com/encode/httpx/blob/0.28.1/httpx/_transports/default.py),
[AnyIO wrapping API](https://github.com/agronholm/anyio/blob/4.10.0/src/anyio/abc/_sockets.py).

### Fresh connections and proxies

`fresh=True` means a fresh connection for every transmitted request, including
redirect hops and retry attempts. It does not promise a never-before-used IP.

- Requests closes returned connections and restores pool capacity with an empty
  slot. Avoid `maxsize=0`, which does not mean no reuse.
- aiohttp uses `force_close=True`.
- HTTPX uses no idle keepalive and requires HTTP/1.1; reject fresh mode with
  HTTP/2 because active connections can multiplex requests.
- Live streamed responses keep their sockets until consumed or closed. Do not
  close active responses to enforce rotation.
- Preserve source mode independently: fresh connections with a fixed or sticky
  policy still reuse the selected IP.

Proxies are outside the supported v1 path. Requests and aiohttp integrations
reject them, and HTTPX transports reject proxy configuration. Examples disable
environment proxies. Document that a separate HTTPX proxy mount or an ordinary
transport mount can bypass the Freebind transport.

## 5. Tests, compatibility, and release gates

Use stdlib `unittest`, `unittest.mock`, and `IsolatedAsyncioTestCase`. Each atomic
task includes its smallest meaningful runnable check; use no new test framework.

### Deterministic tests

Cover prefix normalization and `/0`, `/31`, `/32`, `/63`, `/65`, `/127`, `/128`;
zero/partial/full randomization; invalid counts; preservation of IPv6 when an
integer result is small; mixed families; repeated prefixes; fixed/sticky modes;
and shared sticky policies. Patch randomness to assert exact outcomes rather
than asserting probabilistic uniqueness.

Record socket calls to check options/interface -> Freebind -> bind -> connect.
Inject permission errors, unsupported options, failed binds, DNS failure,
candidate failures, timeout, and cancellation. Check caller/owned descriptor
cleanup and ensure errors never become ordinary-source requests.

Check patch restoration, imported aliases, SSL wrapping before/after connection,
adopted/accepted/duplicated descriptors, creation flags, nonblocking
`connect_ex()`, UDP sends, and explicit adapters under an active patch.

Client tests cover streaming cleanup, retries, redirects, independent concurrent
policies, default pooling, and fresh mode against a keepalive-capable server.
TLS tests verify SNI and certificate checking, including expected failures.

### Real Linux namespace tests

Create disposable client and peer network namespaces joined by a veth pair:

| Role | IPv4 | IPv6 |
| --- | --- | --- |
| Client primary | `10.200.0.1/30` | `fd42:fb::1/64` |
| Peer primary | `10.200.0.2/30` | `fd42:fb::2/64` |
| Client source range | `192.0.2.0/24` | `2001:db8:100::/64` |

Add local routes for source ranges through the client's `lo`; add peer return
routes through the client primary addresses. Bring up loopback and veth links,
wait for IPv6 readiness, and start TCP/UDP echo and HTTP/HTTPS peer servers
reporting source address and connection identity. Use bounded readiness checks,
unique namespace names, and teardown on every exit path.

Source addresses must stay absent from interface address lists. Acceptance
requires the peer to observe the selected source and return traffic. Run clients
without network-administration privileges after setup. Test fixed/random/sticky
policies, both patch timings, both IP families, all adapters, interface selection,
unavailable families, missing routes, fresh connections, and descriptor cleanup.

Privileged setup is test-harness work only. Kernel capability and sandbox limits
must be reported separately from implementation failures.

### Dependency bounds and CI

These are initial supported bounds, not claims of future version compatibility:

| Extra | Initial bounds |
| --- | --- |
| Requests | `requests>=2.34.2,<3`, `urllib3>=2.7,<3` |
| aiohttp | `aiohttp>=3.13.5,<4` |
| HTTPX | `httpx>=0.28.1,<0.29`, `httpcore>=1.0.9,<1.1`, `anyio>=4.10,<5` |
| All | Union of the three extras |

Test CPython 3.11-3.14, minimum and newest allowed dependencies, and an ARM64
Linux smoke run. Unit jobs need no elevated privileges. Private HTTPX/HTTPcore
integration points and aiohttp resolver overrides receive explicit checks.
Broaden bounds only after checks pass; revisit versions at implementation time
without silently expanding the supported API contract.

At least one namespace integration job must pass before release. Skipping the
privileged suite is not release validation. Build v0.1.0 wheel/source artifacts
and install the base package and every extra in clean environments. Verify
license, type marker, imports, and packaged material.

### Licensing

Use GPL-3.0-only for original Python code. The C project is GPL-3.0; freebind.js
is AGPL-3.0. Use the JS project as a behavior reference without copying its
implementation into the GPL-only package. Preserve notices for any permitted
source reuse; no upstream source reuse is required by this design.

Sources: [C license](https://github.com/blechschmidt/freebind/blob/master/LICENSE),
[JS license](https://github.com/imputnet/freebind.js/blob/main/LICENSE),
[GNU compatibility guidance](https://www.gnu.org/licenses/license-compatibility.en.html).

## 6. Defaults and exclusions

- Default source: random per connection; default HTTP behavior: normal pooling.
- Default family policy: strict; ordinary-source fallback requires opt-in and no
  matching-family destination candidate.
- Default programmatic patch: outgoing operation. Environment helper default:
  socket creation.
- Setup failures propagate; do not reproduce C's log-and-continue behavior.
- No import-time activation, automatic route/sysctl/firewall changes, custom DNS
  server, HTTP retry policy, uniqueness allocator, bundled native binary, CFFI
  backend, packet rewriting, custom scheduler, or publication automation.
- Sequential core connection attempts are intentional. Add a `ponytail:` comment
  identifying serial candidate latency and a future Happy Eyeballs upgrade only
  where that implementation choice creates the actual ceiling.
- Explicit helpers/adapters work without a patch. Applications needing libc
  interception use the original Freebind launcher.
