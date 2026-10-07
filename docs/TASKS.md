# Atomic implementation tasks

Behavior is defined in [PLAN.md](PLAN.md). Read [README.md](README.md) for
concurrent-agent rules before claiming work. All tasks below are initially
**pending**: research and documentation are available, but no implementation
task or runtime acceptance gate is complete.

## Ownership and progress

Only the coordinator edits this ledger. Status values: `pending`, `in_progress`,
`review`, `done`, `blocked`. `done` requires integrated code and acceptance
evidence. Record a precise reason for `blocked`; do not equate a skipped check
with success. Owners remain unassigned until actual dispatch.

| ID | Task | Dependencies | Status | Owner | Handoff / revision / blocker |
| --- | --- | --- | --- | --- | --- |
| T01 | Ratify contract and licensing baseline | None | pending | — | — |
| T02 | Package scaffold and optional extras | T01 | pending | — | — |
| T03 | Arbitrary-prefix address generator | T02 | pending | — | — |
| T04 | Source policy and sticky selection | T03 | pending | — | — |
| T05 | Linux Freebind option setup | T02 | pending | — | — |
| T06 | Interface setup and bound socket factories | T04, T05 | pending | — | — |
| T07 | Synchronous connection helper | T06 | pending | — | — |
| T08 | Asyncio connection helper | T07 | pending | — | — |
| T09 | UDP recipes and behavior checks | T06, T08 | pending | — | — |
| T10 | Patch lifecycle and ownership | T04, T06 | pending | — | — |
| T11 | Outgoing-operation patch hooks | T10 | pending | — | — |
| T12 | Socket-construction patch timing | T11 | pending | — | — |
| T13 | Explicit environment helper | T12 | pending | — | — |
| T14 | Requests HTTP/HTTPS adapter | T07 | pending | — | — |
| T15 | Requests fresh connections | T14 | pending | — | — |
| T16 | aiohttp connector | T06 | pending | — | — |
| T17 | HTTPX sync backend and transport | T07 | pending | — | — |
| T18 | HTTPX asyncio backend and transport | T08, T17 | pending | — | — |
| T19 | HTTPX fresh connections | T18 | pending | — | — |
| T20 | Cross-adapter failure and isolation checks | T13, T15, T16, T19 | pending | — | — |
| T21 | Isolated namespace harness | T06 | pending | — | — |
| T22 | End-to-end parity suite | T09, T12, T20, T21 | pending | — | — |
| T23 | Compatibility CI and usage documentation | T22 | pending | — | — |
| T24 | Build and smoke-test release artifacts | T23 | pending | — | — |

## File reservations and interfaces

T02 establishes this compact layout. Paths are ownership boundaries for future
work, not permission to scaffold unused implementation files ahead of their task.

| Area | Reserved implementation / check paths | Tasks that must serialize within area |
| --- | --- | --- |
| Shared package and contract | `pyproject.toml`, `LICENSE`, root `README.md`, `src/freebind/__init__.py`, `src/freebind/py.typed`, `docs/PLAN.md`, `docs/README.md`, `docs/TASKS.md` | Coordinator only, primarily T01/T02/T23/T24 |
| Source selection | `src/freebind/_source.py`, `tests/test_source.py` | T03 then T04 |
| Core sockets | `src/freebind/_socket.py`, `tests/test_socket.py` | T05 then T06 then T07 then T08 |
| UDP recipes | `examples/udp.py`, `tests/test_udp.py` | T09 |
| Monkeypatch | `src/freebind/_patch.py`, `tests/test_patch.py` | T10 then T11 then T12 then T13 |
| Requests | `src/freebind/requests.py`, `tests/test_requests.py` | T14 then T15 |
| aiohttp | `src/freebind/aiohttp.py`, `tests/test_aiohttp.py` | T16 |
| HTTPX | `src/freebind/httpx.py`, `tests/test_httpx.py` | T17 then T18 then T19 |
| Cross-adapter tests | `tests/test_integration_contract.py` | T20 |
| Namespace harness | `tests/network_harness.py` | T21 |
| End-to-end tests | `tests/test_network.py`, test-only TLS certificate material | T22 |
| CI and user examples | `.github/workflows/tests.yml`, `examples/requests_client.py`, `examples/aiohttp_client.py`, `examples/httpx_client.py`, `examples/patch.py` | T23 |
| Build artifacts | Ignored `dist/`, temporary clean-install environments | T24 |

Coordinator edits public exports as each task integrates. Workers import
completed helpers from their owning modules instead of creating competing
helpers. `FamilyMismatchError` and `Source` belong to `_source.py`; socket
primitives belong to `_socket.py`; the patch must use their established behavior.
For internal operations that must bypass an active patch, preserve original
socket operations centrally in `_socket.py`, with the exact internal helper
names established and recorded by T06 before downstream agents start.

Independent checks should remain in the owning area's test file. T20/T22 own
new cross-area tests and must request a reservation before fixing another area's
code. A ready task is not dispatchable while another agent owns its write paths.

## Suggested dispatch sequence

This is a useful schedule, not an additional dependency graph:

1. Coordinator completes T01/T02 and exposes the agreed package/export baseline.
2. T03 and T05 can run concurrently; T04 follows T03. Integrate them, then T06.
3. After T06, T07, T10, T16, and T21 can run concurrently in separate areas.
4. After T07, run T08, T14, and T17 concurrently. Continue T11/T12/T13 in their
   single-owner patch lane. T15 follows T14; T18 waits for both T08 and T17.
5. T09 follows T08; T19 follows T18. T20 waits for completed adapter/patch lanes.
6. T22 integrates the harness and all behavior; T23 establishes compatibility and
   usage evidence; T24 validates the distribution artifacts.

Do not add concurrency by splitting ownership of one module between workers.
The biggest useful parallel block is core async work, patch work, three client
lanes, and the namespace harness after their dependencies become available.

## Task acceptance cards

### T01 — Ratify contract and licensing baseline

**Deliver:** Confirm this plan against the actual implementation environment;
record GPL-3.0-only licensing, upstream commit references, API signatures,
documented behavior differences, and exclusions. Retain JS behavior inspiration
without copying AGPL implementation code. These documents are the input, not an
invitation to research or redesign everything again.

**Check:** No contradictory defaults or missing ownership/error contracts. Any
material contract change is recorded by the coordinator before dependent work.

### T02 — Package scaffold and optional extras

**Deliver:** A `src/` package layout, build metadata, license, typed exports, and
`py.typed`. Declare the optional dependency bounds in PLAN.md. Use one ordinary
build backend; no runtime build requirement or custom packaging framework.
Do not create nonfunctional public placeholder APIs.

**Check:** Base installation imports without clients installed. Metadata contains
Python/Linux/license information and each optional extra independently resolves.

### T03 — Arbitrary-prefix address generator

**Deliver:** `random_ip()` using `ipaddress` and `secrets`, CIDR normalization,
full/partial randomization, validation, and family-preserving integer conversion.

**Check:** Controlled zero/maximum/intermediate draws pass for IPv4/IPv6,
non-byte-aligned prefixes, singleton networks, and `/0`. Invalid counts fail;
large networks are not materialized. Example: `/48`, `bits=8`, draw `0x2c`
produces a `:2c00::` host suffix.

### T04 — Source policy and sticky selection

**Deliver:** `Source` and `FamilyMismatchError`, parsed family-grouped prefixes,
uniform prefix choice, fixed-address singleton behavior, strict selection,
and eagerly selected sticky addresses. Expose `Source.select(family)`.

**Check:** Single/mixed families, repeated entries, absent-family strict versus
non-strict behavior, normalized networks, and fixed/sticky behavior pass.
Sharing a sticky policy retains its selected address without per-call mutation.

### T05 — Linux Freebind option setup

**Deliver:** `enable_freebind()` and platform/constant handling. IPv6 uses its
family option first and falls back only on unsupported-option errors.

**Check:** Recorded socket calls confirm option values and levels. Permission
errors propagate and do not trigger fallback. Non-Linux operation is explicitly
unsupported while imports/address generation remain available.

### T06 — Interface setup and bound socket factories

**Deliver:** `bind_socket()` and TCP/UDP `new_socket()`, family inference,
interface validation/setup, source/port bind, and failure cleanup. Establish the
shared internal original-operation hooks downstream code needs to avoid patch
recursion and double binding.

**Check:** Options/interface and Freebind precede binding. Explicit family
selection, mixed-family inference rejection, invalid interfaces/ports,
already-bound rejection, fallback selection, and owned/caller-owned cleanup
pass. Preserve original-operation helper contracts in the handoff.

### T07 — Synchronous connection helper

**Deliver:** `create_connection()`, resolution, preferred-family filtering,
multiple numeric candidate attempts, socket options, timeout accounting, and
error propagation.

**Check:** Matching candidates are tried in order; an unsupported family does
not win over a configured family. Non-strict fallback occurs only when no
matching candidate exists. DNS and bind failures remain distinguishable; failed
sockets close and the timeout budget does not reset per candidate.

### T08 — Asyncio connection helper

**Deliver:** Async resolution, nonblocking socket connection, and a connected
caller-owned socket with the same selection/failure contract as T07.

**Check:** DNS/connect timeouts and cancellation close owned sockets. Async
resolution does not block the loop. Returned sockets are nonblocking and can be
transferred to a transport; no background connection task is left running.

### T09 — UDP recipes and behavior checks

**Deliver:** A runnable connected/unconnected UDP recipe and tests using
`new_socket(..., type=SOCK_DGRAM)` with ordinary socket and asyncio APIs.
Do not introduce another UDP protocol or packet-rewriting layer.

**Check:** Multiple sends and destinations retain the bound source; replies are
received and ownership/close behavior is correct. Real-network assertions join
the namespace suite at T22.

### T10 — Patch lifecycle and ownership

**Deliver:** `PatchHandle`, method installation on the existing socket class,
single-active-patch enforcement, synchronized state changes, context management,
and safe restoration. Save inherited versus directly defined attributes so
restoration reproduces the original class state.

**Check:** Class identity stays unchanged; nested installation fails; context
exit restores state after exceptions; repeated restoration is harmless; a later
third-party method replacement is not silently overwritten.

### T11 — Outgoing-operation patch hooks

**Deliver:** Prepare unbound sockets before `connect`, `connect_ex`, and first
UDP `sendto`/`sendmsg`. Respect explicit bindings, socket families/types, and
the source contract. Explicit library operations bypass these hooks as needed.

**Check:** Imported constructor aliases and SSLSocket super-calls are covered;
Unix/raw sockets remain untouched; repeated operations do not rebind;
`connect_ex` returns errno on setup errors and preserves nonblocking results.
Explicit source policies under the patch remain authoritative.

### T12 — Socket-construction patch timing

**Deliver:** `entrypoint="socket"`, fresh-descriptor setup, descriptor-adoption
exclusions, and type filtering with creation flags.

**Check:** New eligible sockets bind once; wrapped, accepted, and duplicated
descriptors are not rebound. STREAM/DGRAM filters distinguish actual types.
Tests/documentation expose later explicit-bind conflicts in this timing mode.

### T13 — Explicit environment helper

**Deliver:** `patch_from_env()` with all four variables, C-compatible absent
entrypoint default, prefix splitting, required random sources, and strict input
validation. Keep environment reads out of import-time code.

**Check:** Missing/empty prefixes, malformed tokens, comma/whitespace lists,
case-insensitive documented values, invalid filters/entrypoints, and optional
interface setup pass. No environment mutation or import-time activation occurs.

### T14 — Requests HTTP/HTTPS adapter

**Deliver:** Per-adapter urllib3 manager/pool/connection integration using the
core connector; preserve native TLS/error/auditing behavior. Inject source policy
after pool-key construction and reject proxies.

**Check:** HTTP and HTTPS call the Freebind connector; pool keys contain no
unsupported custom fields; managers do not share mutated class mappings; TLS
configuration and source policy survive connection reuse/reconnection.

### T15 — Requests fresh connections

**Deliver:** Close returned connections in fresh mode and return an empty pool
slot, preserving blocking pool capacity and live response ownership.

**Check:** Sequential requests reconnect against a keepalive server. Streaming,
close-before-consumption, redirects, retries, and blocking pools neither reuse a
prior connection nor leak/deadlock. Normal pooling remains unchanged.

### T16 — aiohttp connector

**Deliver:** `FreebindConnector`, documented socket factory, family filtering
before Happy Eyeballs, proxy/conflict rejection, and `force_close` fresh mode.
Keep resolver cache/tracing and native TLS handling.

**Check:** DNS and numeric hosts, mixed-family answers, strict/non-strict policy,
cached answers, losing candidates, cancellation, TLS, and fresh mode pass.
No factory-created socket is leaked on a setup exception. Numeric hosts with
AF_UNSPEC metadata are classified correctly.

### T17 — HTTPX synchronous backend and transport

**Deliver:** Custom HTTPcore synchronous network backend, existing stream
wrappers, and HTTPX transport initialization with backend injection before use.
Isolate private API references and reject unsupported/conflicting transport
options.

**Check:** Freebind precedes bind/connect; HTTPX request/response streaming and
exception translation remain native. TLS/SNI are preserved. Assert supported
private pool/backend/stream interfaces explicitly.

### T18 — HTTPX asyncio backend and transport

**Deliver:** Async backend using T08, public AnyIO socket wrapping, existing
HTTPcore AnyIO stream/TLS handling, and async transport injection.

**Check:** Cancellation/failure before or during wrapping closes the right
owner's socket. Streaming, TLS, close, client-address inspection, and HTTPX
exception mapping work on asyncio. Do not claim Trio support.

### T19 — HTTPX fresh connections

**Deliver:** No idle keepalive in fresh mode, HTTP/1.1 requirement, and explicit
fresh-plus-HTTP/2 rejection for both transports.

**Check:** Sequential and concurrent requests use distinct connections while
active streams remain valid. Redirects/retry attempts reconnect. Fixed/sticky
source policies retain their IP even when connections are fresh.

### T20 — Cross-adapter failure and isolation checks

**Deliver:** Focused cross-area tests for failure propagation, independent
policies, optional dependency isolation, and adapters/helpers under a patch.
Reserve another area's file before applying fixes exposed by these tests.

**Check:** Option/bind failures never become ordinary-source requests. Different
clients/policies do not share state; a global patch does not double bind explicit
adapters. SDK errors retain underlying causes and no owned descriptor leaks.

### T21 — Isolated namespace harness

**Deliver:** The client/peer/veth topology in PLAN.md, local and return routes,
TCP/UDP and HTTP/HTTPS peer servers, test-only TLS material, bounded readiness,
unique namespace names, and unconditional cleanup. Separate privileged setup
from unprivileged client execution. Expose a harness entrypoint callable by
stdlib tests and CI; no runtime package dependency on this harness.

**Check:** A nonconfigured source can reach the peer and receive a reply;
addresses remain absent from interface lists. Namespace cleanup succeeds after
both successful runs and injected setup/test failure. Record missing kernel or
sandbox capabilities explicitly.

### T22 — End-to-end parity suite

**Deliver:** Real-network checks for raw TCP/UDP, both patch modes, all client
adapters, fixed/random/sticky policies, fresh requests, interface selection,
route failures, family mismatch, and TLS verification.

**Check:** Peer-observed source equals the selected source and return traffic
works for both families. A keepalive-capable server proves default pooling and
fresh-mode differences without relying on caller `Connection: close` headers.
Use controlled draws where different addresses are asserted; uniqueness is not
a probabilistic release assertion.

### T23 — Compatibility CI and usage documentation

**Deliver:** Unit and namespace jobs, CPython 3.11-3.14 coverage, minimum/newest
allowed client dependencies, ARM64 smoke coverage, and runnable usage examples.
Coordinator updates shared exports/root README and records any dependency-bound
changes before merging them. Explain AnyIP/routes, proxy/NAT limitations, patch
lifetime/bypasses, connection reuse, and source uniqueness limits.

**Check:** Documented supported combinations pass. At least one namespace job
must actually pass for release; a skipped privileged suite is insufficient.
Examples cover each client, UDP, patching, and original-launcher alternatives.

### T24 — Build and smoke-test release artifacts

**Deliver:** v0.1.0 wheel/source artifacts with metadata, license, typing marker,
and all intended package files. Build in the normal ignored artifact directory
and test clean installations; do not publish automatically.

**Check:** Base and every extra install independently, exports import, optional
clients stay optional, and packaged material is correct. Record exact successful
checks and unresolved limitations in the final handoff. This task requires T23's
real integration evidence rather than treating skipped tests as a release pass.
