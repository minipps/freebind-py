"""Disposable Linux network namespaces for real Freebind tests.

Run ``python tests/network_harness.py -- <client command>``. Unprivileged
developer runs bootstrap a private user, mount, and network namespace; root CI
can run the same harness directly when it has CAP_NET_ADMIN and CAP_SYS_ADMIN.
Only the setup process has those capabilities. The client command runs in the
client namespace with every capability dropped.
"""

from __future__ import annotations

import argparse
from http.client import HTTPConnection, HTTPResponse
from http.server import BaseHTTPRequestHandler
import itertools
import json
import os
from pathlib import Path
import secrets
import select
import shutil
import socket
import socketserver
import ssl
import subprocess
import sys
import threading
import time
from typing import Sequence
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
CERT_DIR = Path(__file__).resolve().parent / "certs"
CA_FILE = CERT_DIR / "ca.pem"
SERVER_KEY = CERT_DIR / "server.key"
CLIENT4 = "10.200.0.1"
PEER4 = "10.200.0.2"
SOURCE4 = "192.0.2.0/24"
CLIENT6 = "fd42:fb::1"
PEER6 = "fd42:fb::2"
SOURCE6 = "2001:db8:100::/64"
PORTS = {"tcp": 18401, "udp": 18402, "http": 18403, "https": 18443}
_CAP_CHECK_MARKER = "FREEBIND_HARNESS_CAPS="


def _run(command: Sequence[str], *, timeout: float = 10, check: bool = True, **kwargs):
    result = subprocess.run(command, timeout=timeout, check=False, **kwargs)
    if check and result.returncode:
        stderr = getattr(result, "stderr", b"") or b""
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors="replace")
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(command)}\n{stderr}")
    return result


def _capabilities() -> dict[str, int]:
    fields = {"CapEff", "CapPrm", "CapInh", "CapBnd", "CapAmb"}
    values = {}
    with open("/proc/self/status", encoding="ascii") as status:
        for line in status:
            name, sep, value = line.partition(":")
            if sep and name in fields:
                values[name] = int(value.strip(), 16)
    return values


def _has_capability(name: str) -> bool:
    bit = {"CAP_NET_ADMIN": 12, "CAP_SYS_ADMIN": 21}[name]
    return bool(_capabilities().get("CapEff", 0) & (1 << bit))


def _client_exec(command: Sequence[str]) -> int:
    if not command:
        print("client command is required", file=sys.stderr)
        return 2
    caps = _capabilities()
    report = {
        "uid": os.getuid(),
        "euid": os.geteuid(),
        "capabilities": caps,
    }
    print(_CAP_CHECK_MARKER + json.dumps(report, sort_keys=True), file=sys.stderr, flush=True)
    if any(caps.get(name, 0) for name in ("CapEff", "CapPrm", "CapInh", "CapBnd", "CapAmb")):
        print("client still has capabilities after setpriv", file=sys.stderr)
        return 126
    os.execvp(command[0], command)
    return 127


class _ThreadingServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, handler, family, tls_context=None):
        self.address_family = family
        self.connection_ids = itertools.count(1)
        super().__init__(address, handler)
        if tls_context is not None:
            self.socket = tls_context.wrap_socket(self.socket, server_side=True)

    def server_bind(self):
        if self.address_family == socket.AF_INET6:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        super().server_bind()


def _response(handler) -> bytes:
    address = handler.client_address
    return json.dumps(
        {
            "source": address[0].split("%", 1)[0],
            "source_port": address[1],
            "connection_id": handler.connection_id,
            "tls_server_name": getattr(handler.connection, "freebind_server_name", None),
        },
        sort_keys=True,
    ).encode() + b"\n"


class _EchoHandler(socketserver.StreamRequestHandler):
    def setup(self):
        super().setup()
        self.connection_id = next(self.server.connection_ids)

    def handle(self):
        for _line in iter(self.rfile.readline, b""):
            self.wfile.write(_response(self))


class _UDPHandler(socketserver.BaseRequestHandler):
    def handle(self):
        _data, sock = self.request
        source = self.client_address
        payload = json.dumps(
            {
                "source": source[0].split("%", 1)[0],
                "source_port": source[1],
                "connection_id": f"{source[0]}:{source[1]}",
            },
            sort_keys=True,
        ).encode()
        sock.sendto(payload, source)


class _HTTPHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.connection_id = next(self.server.connection_ids)

    def _respond(self, *, head=False):
        body = _response(self)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def do_GET(self):
        self._respond()

    def do_HEAD(self):
        self._respond(head=True)

    def log_message(self, _format, *args):
        pass


class _UDPServer(socketserver.ThreadingUDPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, handler, family):
        self.address_family = family
        super().__init__(address, handler)

    def server_bind(self):
        if self.address_family == socket.AF_INET6:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        super().server_bind()


def _serve_peer() -> int:
    servers = []
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(CERT_DIR / "ca.pem", SERVER_KEY)

        def remember_server_name(sock, server_name, _context):
            sock.freebind_server_name = server_name

        context.sni_callback = remember_server_name
        endpoints = ((socket.AF_INET, PEER4), (socket.AF_INET6, PEER6))
        for family, host in endpoints:
            for kind, handler in (("tcp", _EchoHandler), ("http", _HTTPHandler), ("https", _HTTPHandler)):
                tls = context if kind == "https" else None
                servers.append(_ThreadingServer((host, PORTS[kind]), handler, family, tls))
            servers.append(_UDPServer((host, PORTS["udp"]), _UDPHandler, family))
        for server in servers:
            threading.Thread(target=server.serve_forever, daemon=True).start()
        print(json.dumps({"ready": True, "ports": PORTS}), flush=True)
        while True:
            time.sleep(3600)
    except BaseException as exc:
        print(json.dumps({"error": str(exc)}), flush=True)
        return 1
    finally:
        for server in servers:
            server.server_close()


class NetworkHarness:
    """Create and own a client/peer veth pair plus source and return routes."""

    def __init__(self, *, ready_timeout: float = 8, prefix: str = "freebind"):
        if ready_timeout <= 0:
            raise ValueError("ready_timeout must be positive")
        if not prefix or len(prefix) > 64 or any(not (char.isalnum() or char in "-_") for char in prefix):
            raise ValueError("prefix must contain only letters, digits, hyphens, and underscores")
        self.ready_timeout = ready_timeout
        token = secrets.token_hex(6)
        self.client_ns = f"{prefix}-c-{token}"
        self.peer_ns = f"{prefix}-p-{token}"
        self._ip = shutil.which("ip") or "ip"
        self._names: list[str] = []
        self._peer: subprocess.Popen | None = None
        self._ports = dict(PORTS)
        self._ready = False
        self.last_capabilities = None

    def __enter__(self):
        return self.setup()

    def __exit__(self, exc_type, exc, traceback):
        try:
            self.close()
        except Exception as cleanup_error:
            if exc is None:
                raise
            if hasattr(exc, "add_note"):
                exc.add_note(f"network harness cleanup also failed: {cleanup_error}")
        return False

    def _ip_run(self, *args: str, timeout: float = 10, check: bool = True):
        return _run([self._ip, *args], timeout=timeout, check=check, capture_output=True)

    def _ip_ns(self, namespace: str, *args: str):
        return self._ip_run("-n", namespace, *args)

    def _wait_ipv6(self):
        deadline = time.monotonic() + self.ready_timeout
        for namespace, address in ((self.client_ns, CLIENT6), (self.peer_ns, PEER6)):
            while time.monotonic() < deadline:
                result = self._ip_run("-6", "-n", namespace, "-o", "addr", "show", "dev", "eth0", check=False)
                lines = result.stdout.decode(errors="replace").splitlines()
                if any(address in line and "tentative" not in line for line in lines):
                    break
                time.sleep(0.05)
            else:
                raise TimeoutError(f"IPv6 address {address} was not ready in {namespace}")

    def setup(self):
        if not (_has_capability("CAP_NET_ADMIN") and _has_capability("CAP_SYS_ADMIN")):
            raise RuntimeError(
                "namespace setup needs CAP_NET_ADMIN and CAP_SYS_ADMIN; run via the harness CLI "
                "to use its private user/mount/network bootstrap"
            )
        if not CA_FILE.is_file() or not SERVER_KEY.is_file():
            raise RuntimeError(f"test TLS material is missing under {CERT_DIR}")
        try:
            for name in (self.client_ns, self.peer_ns):
                self._ip_run("netns", "add", name)
                self._names.append(name)
            for namespace in (self.client_ns, self.peer_ns):
                self._ip_ns(namespace, "link", "set", "lo", "up")
            self._ip_ns(self.client_ns, "link", "add", "fb0", "type", "veth", "peer", "name", "fb1")
            self._ip_ns(self.client_ns, "link", "set", "fb1", "netns", self.peer_ns)
            self._ip_ns(self.client_ns, "link", "set", "fb0", "name", "eth0")
            self._ip_ns(self.peer_ns, "link", "set", "fb1", "name", "eth0")
            for namespace in (self.client_ns, self.peer_ns):
                self._ip_ns(namespace, "link", "set", "eth0", "up")
            self._ip_ns(self.client_ns, "addr", "add", f"{CLIENT4}/30", "dev", "eth0")
            self._ip_ns(self.peer_ns, "addr", "add", f"{PEER4}/30", "dev", "eth0")
            self._ip_run("-6", "-n", self.client_ns, "addr", "add", f"{CLIENT6}/64", "dev", "eth0", "nodad")
            self._ip_run("-6", "-n", self.peer_ns, "addr", "add", f"{PEER6}/64", "dev", "eth0", "nodad")
            self._wait_ipv6()
            self._ip_ns(self.client_ns, "route", "add", "local", SOURCE4, "dev", "lo")
            self._ip_run("-6", "-n", self.client_ns, "route", "add", "local", SOURCE6, "dev", "lo")
            self._ip_ns(self.peer_ns, "route", "add", SOURCE4, "via", CLIENT4, "dev", "eth0")
            self._ip_run("-6", "-n", self.peer_ns, "route", "add", SOURCE6, "via", CLIENT6, "dev", "eth0")
            self._start_peer()
            self._ready = True
            self._wait_peer()
            return self
        except BaseException as setup_error:
            try:
                self.close()
            except Exception as cleanup_error:
                if hasattr(setup_error, "add_note"):
                    setup_error.add_note(f"network harness cleanup also failed: {cleanup_error}")
            raise

    def _start_peer(self):
        command = [
            self._ip,
            "netns",
            "exec",
            self.peer_ns,
            shutil.which("setpriv") or "setpriv",
            "--bounding-set=-all",
            "--inh-caps=-all",
            "--ambient-caps=-all",
            "--no-new-privs",
            sys.executable,
            str(Path(__file__).resolve()),
            "--_serve-peer",
        ]
        self._peer = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        assert self._peer.stdout is not None
        ready, _, _ = select.select([self._peer.stdout], [], [], self.ready_timeout)
        if not ready:
            raise TimeoutError(f"peer servers did not become ready within {self.ready_timeout:g}s")
        line = self._peer.stdout.readline()
        try:
            status = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"invalid peer readiness response: {line!r}") from exc
        if "error" in status:
            raise RuntimeError(f"peer server startup failed: {status['error']}")
        self._ports.update(status.get("ports", {}))

    def _wait_peer(self):
        # Resolve primary veth neighbors before off-link source traffic; first IPv6 ND can stall otherwise.
        deadline = time.monotonic() + self.ready_timeout
        last_error = ""
        while time.monotonic() < deadline:
            try:
                result = self.run_client(
                    [sys.executable, str(Path(__file__).resolve()), "--_warmup-client"],
                    timeout=min(3, max(0.1, deadline - time.monotonic())),
                )
                if result.returncode == 0:
                    return
                last_error = result.stderr
            except (OSError, subprocess.SubprocessError) as exc:
                last_error = str(exc)
            time.sleep(0.05)
        raise TimeoutError(f"primary peer connectivity was not ready: {last_error}")

    def environment(self) -> dict[str, str]:
        """Return destination, source-range, port, and TLS variables for clients."""
        p = self._ports
        return {
            "FREEBIND_HARNESS_CLIENT4": CLIENT4,
            "FREEBIND_HARNESS_PEER4": PEER4,
            "FREEBIND_HARNESS_SOURCE4": SOURCE4,
            "FREEBIND_HARNESS_CLIENT6": CLIENT6,
            "FREEBIND_HARNESS_PEER6": PEER6,
            "FREEBIND_HARNESS_SOURCE6": SOURCE6,
            "FREEBIND_HARNESS_TCP_PORT": str(p["tcp"]),
            "FREEBIND_HARNESS_UDP_PORT": str(p["udp"]),
            "FREEBIND_HARNESS_HTTP_PORT": str(p["http"]),
            "FREEBIND_HARNESS_HTTPS_PORT": str(p["https"]),
            "FREEBIND_HARNESS_HTTP4_URL": f"http://{PEER4}:{p['http']}/report",
            "FREEBIND_HARNESS_HTTP6_URL": f"http://[{PEER6}]:{p['http']}/report",
            "FREEBIND_HARNESS_HTTPS4_URL": f"https://{PEER4}:{p['https']}/report",
            "FREEBIND_HARNESS_HTTPS6_URL": f"https://[{PEER6}]:{p['https']}/report",
            "FREEBIND_HARNESS_CA_FILE": str(CA_FILE),
            "FREEBIND_HARNESS_CLIENT_NS": self.client_ns,
            "FREEBIND_HARNESS_PEER_NS": self.peer_ns,
            "FREEBIND_HARNESS_CAPS_DROPPED": "1",
        }

    @property
    def ports(self) -> dict[str, int]:
        return dict(self._ports)

    def run_client(
        self,
        command: Sequence[str],
        *,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
        check: bool = False,
    ) -> subprocess.CompletedProcess:
        if not self._ready:
            raise RuntimeError("harness must be set up before running clients")
        if not command:
            raise ValueError("client command cannot be empty")
        client_env = os.environ.copy()
        for key in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            client_env.pop(key, None)
        client_env.update(self.environment())
        if env:
            client_env.update(env)
        wrapper = [
            self._ip,
            "netns",
            "exec",
            self.client_ns,
            shutil.which("setpriv") or "setpriv",
            "--bounding-set=-all",
            "--inh-caps=-all",
            "--ambient-caps=-all",
            "--no-new-privs",
            sys.executable,
            str(Path(__file__).resolve()),
            "--_client-exec",
            "--",
            *command,
        ]
        result = subprocess.run(wrapper, env=client_env, timeout=timeout, capture_output=True, text=True)
        lines = result.stderr.splitlines()
        marker = next((line[len(_CAP_CHECK_MARKER):] for line in lines if line.startswith(_CAP_CHECK_MARKER)), None)
        if marker is None:
            raise RuntimeError(f"client privilege check did not run; stderr:\n{result.stderr}")
        self.last_capabilities = json.loads(marker)
        if check and result.returncode:
            raise subprocess.CalledProcessError(result.returncode, command, result.stdout, result.stderr)
        return result

    def smoke(self):
        """Prove IPv4/IPv6 Freebind source routing and return traffic end to end."""
        try:
            result = self.run_client(
                [sys.executable, str(Path(__file__).resolve()), "--_smoke-client"],
                check=True,
                timeout=self.ready_timeout,
            )
        except Exception as exc:
            details = ""
            if isinstance(exc, subprocess.CalledProcessError):
                details = exc.stderr or exc.output or ""
            raise RuntimeError(f"{exc}\n{details}\n{self.diagnostics()}") from exc
        reports = json.loads(result.stdout)
        expected = {"ipv4": "192.0.2.7", "ipv6": "2001:db8:100::7"}
        if {key: reports[key]["source"] for key in expected} != expected:
            raise AssertionError(f"peer saw unexpected sources: {reports}")
        for key, source in expected.items():
            if reports[key]["local_source"] != source or not reports[key]["connection_id"]:
                raise AssertionError(f"source bind/return check failed for {key}: {reports[key]}")
        addresses = self._ip_run("-n", self.client_ns, "addr", "show").stdout.decode(errors="replace")
        if "192.0.2." in addresses or "2001:db8:100:" in addresses:
            raise AssertionError("source ranges unexpectedly appear on a client interface")
        return reports

    def diagnostics(self) -> str:
        output = []
        for namespace in (self.client_ns, self.peer_ns):
            output.append(f"[{namespace}]")
            for args in (("addr", "show"), ("route", "show", "table", "all"), ("-6", "route", "show", "table", "all")):
                result = self._ip_run("-n", namespace, *args, check=False)
                output.append(result.stdout.decode(errors="replace").strip())
            for key in ("all", "eth0"):
                path = f"/proc/sys/net/ipv4/conf/{key}/rp_filter"
                result = self._ip_run("netns", "exec", namespace, "cat", path, check=False)
                output.append(f"{path}: {result.stdout.decode(errors='replace').strip()}")
        return "\n".join(output)

    def close(self):
        errors = []
        peer = self._peer
        if peer is not None:
            try:
                if peer.poll() is None:
                    peer.terminate()
                    try:
                        peer.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        peer.kill()
                        peer.wait(timeout=2)
                self._peer = None
            except Exception as exc:
                errors.append(exc)
            finally:
                for stream in (peer.stdout, peer.stderr):
                    if stream is not None:
                        stream.close()
        remaining = []
        for name in reversed(self._names):
            try:
                result = self._ip_run("netns", "del", name, check=False, timeout=5)
                if result.returncode:
                    remaining.append(name)
                    errors.append(RuntimeError(f"ip netns del {name} exited {result.returncode}"))
            except Exception as exc:
                errors.append(exc)
                remaining.append(name)
        self._names = list(reversed(remaining))
        self._ready = False
        if errors:
            raise RuntimeError(f"network harness cleanup had {len(errors)} error(s)") from errors[0]


def _smoke_client() -> int:
    sys.path.insert(0, str(ROOT / "src"))
    from freebind import Source, new_socket

    reports = {}
    with socket.create_connection((PEER4, PORTS["tcp"]), timeout=2) as ordinary:
        ordinary.sendall(b"report\n")
        with ordinary.makefile("rb") as reader:
            reports["ordinary"] = json.loads(reader.readline())
    with socket.create_connection((PEER6, PORTS["tcp"]), timeout=2) as ordinary:
        ordinary.sendall(b"report\n")
        with ordinary.makefile("rb") as reader:
            reports["ordinary6"] = json.loads(reader.readline())
    for key, family, source, peer in (
        ("ipv4", socket.AF_INET, "192.0.2.7", PEER4),
        ("ipv6", socket.AF_INET6, "2001:db8:100::7", PEER6),
    ):
        sock = new_socket(Source(source), family=family, type=socket.SOCK_STREAM)
        try:
            sock.settimeout(2)
            try:
                sock.connect((peer, PORTS["tcp"]))
            except OSError as exc:
                route_args = ["ip"]
                if family == socket.AF_INET6:
                    route_args.append("-6")
                route = subprocess.run(
                    [*route_args, "route", "get", peer, "from", source],
                    capture_output=True,
                    text=True,
                )
                raise RuntimeError(
                    f"{key} connect failed local={sock.getsockname()} peer={peer}; "
                    f"route={route.stdout.strip()} {route.stderr.strip()}: {exc}"
                ) from exc
            sock.sendall(b"report\n")
            with sock.makefile("rb") as reader:
                remote = json.loads(reader.readline())
            reports[key] = {**remote, "local_source": sock.getsockname()[0]}
        finally:
            sock.close()

    for key, family, peer in (("udp4", socket.AF_INET, PEER4), ("udp6", socket.AF_INET6, PEER6)):
        with socket.socket(family, socket.SOCK_DGRAM) as sock:
            sock.settimeout(2)
            sock.sendto(b"report", (peer, PORTS["udp"]))
            reports[key] = json.loads(sock.recvfrom(4096)[0])

    http = HTTPConnection(PEER4, PORTS["http"], timeout=2)
    try:
        for index in range(2):
            http.request("GET", "/report")
            response = http.getresponse()
            reports[f"http{index}"] = json.loads(response.read())
    finally:
        http.close()

    context = ssl.create_default_context(cafile=str(CA_FILE))
    raw = socket.create_connection((PEER4, PORTS["https"]), timeout=2)
    with context.wrap_socket(raw, server_hostname="peer.freebind.test") as tls:
        for index in range(2):
            tls.sendall(b"GET /report HTTP/1.1\r\nHost: peer.freebind.test\r\n\r\n")
            response = HTTPResponse(tls)
            response.begin()
            reports[f"https{index}"] = json.loads(response.read())
            response.close()
    for key, expected in (("udp4", CLIENT4), ("udp6", CLIENT6), ("http0", CLIENT4), ("http1", CLIENT4), ("https0", CLIENT4), ("https1", CLIENT4)):
        if reports[key]["source"] != expected:
            raise AssertionError(f"{key} peer source mismatch: {reports[key]}")
    if reports["http0"]["connection_id"] != reports["http1"]["connection_id"]:
        raise AssertionError("HTTP keepalive did not reuse its connection")
    if reports["https0"]["connection_id"] != reports["https1"]["connection_id"]:
        raise AssertionError("HTTPS keepalive did not reuse its connection")
    if any(reports[key]["tls_server_name"] != "peer.freebind.test" for key in ("https0", "https1")):
        raise AssertionError("HTTPS peer did not receive the expected SNI name")
    print(json.dumps(reports, sort_keys=True))
    return 0


def _warmup_client() -> int:
    for peer, expected in ((PEER4, CLIENT4), (PEER6, CLIENT6)):
        with socket.create_connection((peer, PORTS["tcp"]), timeout=1) as sock:
            sock.sendall(b"report\n")
            with sock.makefile("rb") as reader:
                report = json.loads(reader.readline())
            if report["source"] != expected:
                raise AssertionError(f"peer saw {report['source']}, expected {expected}")
    return 0


def _cleanup_self_check():
    """Inject one namespace deletion failure and verify all cleanup is attempted."""

    class FakePeer:
        def __init__(self):
            self.stopped = False
            self.stdout = self.stderr = None

        def poll(self):
            return None if not self.stopped else 0

        def terminate(self):
            self.stopped = True

        def wait(self, timeout=None):
            return 0

        def kill(self):
            self.stopped = True

    harness = object.__new__(NetworkHarness)
    harness._peer = FakePeer()
    harness._ip = "ip"
    harness._names = ["client", "peer"]
    harness._ready = True
    calls = []

    def injected(command, **kwargs):
        calls.append(command)
        if command[-1] == "peer":
            raise OSError("injected delete failure")
        return subprocess.CompletedProcess(command, 0, b"", b"")

    with patch("subprocess.run", side_effect=injected):
        try:
            harness.close()
        except RuntimeError:
            pass
        else:
            raise AssertionError("injected cleanup failure was not reported")
    assert [command[-1] for command in calls] == ["peer", "client"]
    assert harness._peer is None and harness._ready is False
    assert harness._names == ["peer"]


def _setup_failure_self_check():
    harness = NetworkHarness()
    calls = []

    def injected(*args, **kwargs):
        calls.append(args)
        if args[:3] == ("-n", harness.client_ns, "link"):
            raise OSError("injected setup failure")
        return subprocess.CompletedProcess(args, 0, b"", b"")

    with patch(__name__ + "._has_capability", return_value=True), patch.object(harness, "_ip_run", side_effect=injected):
        try:
            harness.setup()
        except OSError as exc:
            assert str(exc) == "injected setup failure"
        else:
            raise AssertionError("injected setup failure was not raised")
    assert calls == [
        ("netns", "add", harness.client_ns),
        ("netns", "add", harness.peer_ns),
        ("-n", harness.client_ns, "link", "set", "lo", "up"),
        ("netns", "del", harness.peer_ns),
        ("netns", "del", harness.client_ns),
    ]
    assert not harness._names and harness._peer is None


def _cleanup_failure_smoke(timeout, prefix):
    harness = NetworkHarness(ready_timeout=timeout, prefix=prefix)
    names = (harness.client_ns, harness.peer_ns)
    try:
        with harness:
            harness.run_client(
                [sys.executable, "-c", "raise SystemExit(19)"],
                timeout=timeout,
                check=True,
            )
    except subprocess.CalledProcessError as exc:
        if exc.returncode != 19:
            raise
    else:
        raise AssertionError("injected client failure did not occur")
    _assert_names_gone(harness._ip, names)
    print("client-failure namespace cleanup passed")
    return 0


def _assert_names_gone(ip, names):
    listed = _run([ip, "netns", "list"], capture_output=True, text=True)
    remaining = set(names).intersection(line.split()[0] for line in listed.stdout.splitlines() if line.split())
    if remaining:
        raise AssertionError(f"namespaces remained after harness exit: {sorted(remaining)}")


def _run_harness(args, command, *, private_mount=False):
    created_netns_dir = False
    try:
        if private_mount:
            _run(["mount", "--make-rprivate", "/"])
            _run(["mount", "-t", "tmpfs", "-o", "mode=755,nosuid,nodev", "tmpfs", "/run"])
        else:
            created_netns_dir = not Path("/run/netns").exists()
        Path("/run/netns").mkdir(parents=True, exist_ok=True)
        if args.cleanup_smoke:
            return _cleanup_failure_smoke(args.timeout, args.prefix)
        harness = NetworkHarness(ready_timeout=args.timeout, prefix=args.prefix)
        names = (harness.client_ns, harness.peer_ns)
        with harness:
            if args.smoke:
                print(json.dumps(harness.smoke(), sort_keys=True))
                status = 0
            else:
                result = harness.run_client(command, timeout=args.client_timeout)
                sys.stdout.write(result.stdout)
                sys.stderr.write(result.stderr)
                status = result.returncode
        _assert_names_gone(harness._ip, names)
        return status
    except Exception as exc:
        if isinstance(exc, subprocess.CalledProcessError):
            output = exc.stderr or exc.output or ""
            print(f"client command failed with exit status {exc.returncode}:\n{output}", file=sys.stderr)
            return exc.returncode
        print(f"namespace harness unavailable or failed: {exc}", file=sys.stderr)
        return 2
    finally:
        if created_netns_dir:
            try:
                Path("/run/netns").rmdir()
            except OSError:
                pass


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--_serve-peer":
        return _serve_peer()
    if argv and argv[0] == "--_client-exec":
        return _client_exec(argv[2:] if len(argv) > 1 and argv[1] == "--" else argv[1:])
    if argv and argv[0] == "--_smoke-client":
        return _smoke_client()
    if argv and argv[0] == "--_warmup-client":
        return _warmup_client()
    if argv and argv[0] == "--_bootstrap":
        args = argparse.Namespace(
            timeout=float(argv[1]),
            prefix=argv[2],
            client_timeout=None if argv[3] == "-" else float(argv[3]),
            smoke=argv[4] == "1",
            cleanup_smoke=argv[5] == "1",
        )
        return _run_harness(args, argv[7:], private_mount=True)

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=8, help="bounded peer readiness timeout")
    parser.add_argument("--client-timeout", type=float, help="optional whole client command timeout")
    parser.add_argument("--prefix", default="freebind", help="prefix for unique namespace names")
    parser.add_argument("--smoke", action="store_true", help="run real IPv4/IPv6 source-and-return checks")
    parser.add_argument("--self-check", action="store_true", help="run injected unconditional-cleanup check")
    parser.add_argument("--cleanup-smoke", action="store_true", help="verify actual namespace teardown after client failure")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="client command after --")
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if args.self_check:
        _cleanup_self_check()
        _setup_failure_self_check()
        print("injected cleanup checks passed")
        if not (args.smoke or command):
            return 0
    if not (args.smoke or args.self_check or args.cleanup_smoke) and not command:
        parser.error("provide a smoke check or a client command after --")

    if _has_capability("CAP_NET_ADMIN") and _has_capability("CAP_SYS_ADMIN"):
        return _run_harness(args, command)
    unshare = shutil.which("unshare")
    if not unshare:
        print("namespace harness requires iproute2 and unshare", file=sys.stderr)
        return 2
    internal = [
        unshare,
        "--user",
        "--map-root-user",
        "--net",
        "--mount",
        "--fork",
        "--kill-child",
        "--forward-signals",
        sys.executable,
        str(Path(__file__).resolve()),
        "--_bootstrap",
        str(args.timeout),
        args.prefix,
        str(args.client_timeout) if args.client_timeout is not None else "-",
        "1" if args.smoke else "0",
        "1" if args.cleanup_smoke else "0",
        "--",
        *command,
    ]
    result = subprocess.run(internal, timeout=None, check=False)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
