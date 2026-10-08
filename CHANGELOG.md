# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-10-08

### Added

- Optional `curl-cffi` extra with `FreebindSession` and `AsyncFreebindSession`
  for source-bound requests with curl_cffi browser impersonation.
- Support for pooled and fresh connections, streaming, redirects, and concurrent
  asyncio requests; binding errors retain their original cause on buffered requests.
- curl_cffi sync/async example, documentation, and unit and IPv4/IPv6 namespace
  coverage, with curl_cffi included in the compatibility workflow.

## [0.1.1] - 2026-10-08

### Changed

- Credit the original Freebind project and explain IPv6 address and prefix
  rotation, rate-limit scope, routing, and connection freshness.
- Use IPv6 in the README, user guide, and runnable example instructions.

## [0.1.0] - 2026-10-08

### Added

- Initial release of Linux Freebind support for IPv4/IPv6 TCP and UDP sockets
  on Python 3.11 and newer, with no required third-party dependencies.
- Source policies for fixed addresses and CIDR prefixes, random or sticky
  address selection, and control over randomized host bits.
- Synchronous and asyncio socket creation and connection helpers.
- Optional Requests adapter, aiohttp connector, and synchronous and asynchronous
  HTTPX transports, with connection pooling and fresh-connection support.
- Reversible process-wide socket patching and environment-based configuration.
- Type information, user documentation, and runnable examples.

[Unreleased]: https://github.com/minipps/freebind-py/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/minipps/freebind-py/compare/v0.1.1...v0.2.0
[0.1.0]: https://github.com/minipps/freebind-py/releases/tag/v0.1.0
[0.1.1]: https://github.com/minipps/freebind-py/compare/v0.1.0...v0.1.1
