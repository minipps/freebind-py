import asyncio
import socket
import sys
import unittest

from freebind import Source, new_socket


SOURCE = "127.0.0.2"


class _Echo(asyncio.DatagramProtocol):
    def connection_made(self, transport):
        self.transport = transport
        self.sources = asyncio.Queue()

    def datagram_received(self, data, address):
        self.sources.put_nowait((data, address))
        self.transport.sendto(b"reply:" + data, address)


class _Replies(asyncio.DatagramProtocol):
    def __init__(self):
        self.received = asyncio.Queue()
        self.closed = asyncio.get_running_loop().create_future()

    def datagram_received(self, data, address):
        self.received.put_nowait((data, address))

    def connection_lost(self, error):
        if not self.closed.done():
            self.closed.set_result(error)


@unittest.skipUnless(sys.platform.startswith("linux"), "Freebind requires Linux")
class UDPTests(unittest.TestCase):
    def test_unconnected_socket_reuses_bound_source_across_peers(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as first, socket.socket(
            socket.AF_INET, socket.SOCK_DGRAM
        ) as second:
            first.bind(("127.0.0.1", 0))
            second.bind(("127.0.0.3", 0))
            first.settimeout(1)
            second.settimeout(1)
            peers = (first, second)
            with new_socket(Source(SOURCE), type=socket.SOCK_DGRAM) as client:
                local = client.getsockname()
                self.assertEqual(local[0], SOURCE)
                for index, peer in enumerate(peers):
                    message = f"packet {index}".encode()
                    client.sendto(message, peer.getsockname())
                    received, sender = peer.recvfrom(1024)
                    self.assertEqual(received, message)
                    self.assertEqual(sender, (SOURCE, local[1]))
                    peer.sendto(b"reply:" + received, sender)
                    reply, reply_from = client.recvfrom(1024)
                    self.assertEqual(reply, b"reply:" + message)
                    self.assertEqual(reply_from, peer.getsockname())
                    self.assertEqual(client.getsockname(), local)
            self.assertEqual(client.fileno(), -1)

    def test_connected_socket_sends_and_receives(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as peer:
            peer.bind(("127.0.0.1", 0))
            peer.settimeout(1)
            with new_socket(Source(SOURCE), type=socket.SOCK_DGRAM) as client:
                client.settimeout(1)
                client.connect(peer.getsockname())
                local = client.getsockname()
                client.send(b"connected")
                message, sender = peer.recvfrom(1024)
                self.assertEqual(message, b"connected")
                self.assertEqual(sender, (SOURCE, local[1]))
                peer.sendto(b"reply", sender)
                self.assertEqual(client.recv(1024), b"reply")
                self.assertEqual(client.getsockname(), local)
            self.assertEqual(client.fileno(), -1)


@unittest.skipUnless(sys.platform.startswith("linux"), "Freebind requires Linux")
class AsyncUDPTests(unittest.IsolatedAsyncioTestCase):
    async def test_datagram_transport_reuses_bound_source_and_closes_socket(self):
        loop = asyncio.get_running_loop()
        peers = []
        for host in ("127.0.0.1", "127.0.0.3"):
            transport, protocol = await loop.create_datagram_endpoint(
                _Echo, local_addr=(host, 0), family=socket.AF_INET
            )
            peers.append((transport, protocol))

        sock = new_socket(Source(SOURCE), type=socket.SOCK_DGRAM)
        local = sock.getsockname()
        client_transport, client_protocol = await loop.create_datagram_endpoint(
            _Replies, sock=sock
        )
        try:
            self.assertEqual(local[0], SOURCE)
            for index, (peer_transport, peer_protocol) in enumerate(peers):
                address = peer_transport.get_extra_info("sockname")
                message = f"async packet {index}".encode()
                client_transport.sendto(message, address)
                received, sender = await asyncio.wait_for(
                    peer_protocol.sources.get(), 1
                )
                self.assertEqual(sender, (SOURCE, local[1]))
                self.assertEqual(received, message)
                reply, reply_from = await asyncio.wait_for(
                    client_protocol.received.get(), 1
                )
                self.assertEqual(reply, b"reply:" + message)
                self.assertEqual(reply_from, address)
                self.assertEqual(sock.getsockname(), local)
        finally:
            client_transport.close()
            for peer_transport, _ in peers:
                peer_transport.close()
        await asyncio.wait_for(client_protocol.closed, 1)
        self.assertEqual(sock.fileno(), -1)

    async def test_connected_datagram_transport_sends_and_receives(self):
        loop = asyncio.get_running_loop()
        peer_transport, peer_protocol = await loop.create_datagram_endpoint(
            _Echo, local_addr=("127.0.0.1", 0), family=socket.AF_INET
        )
        address = peer_transport.get_extra_info("sockname")
        sock = new_socket(Source(SOURCE), type=socket.SOCK_DGRAM)
        sock.connect(address)
        local = sock.getsockname()
        client_transport, client_protocol = await loop.create_datagram_endpoint(
            _Replies, sock=sock
        )
        try:
            client_transport.sendto(b"async connected")
            received, sender = await asyncio.wait_for(peer_protocol.sources.get(), 1)
            self.assertEqual(received, b"async connected")
            self.assertEqual(sender, (SOURCE, local[1]))
            reply, reply_from = await asyncio.wait_for(
                client_protocol.received.get(), 1
            )
            self.assertEqual(reply, b"reply:async connected")
            self.assertEqual(reply_from, address)
        finally:
            client_transport.close()
            peer_transport.close()
        await asyncio.wait_for(client_protocol.closed, 1)
        self.assertEqual(sock.fileno(), -1)
