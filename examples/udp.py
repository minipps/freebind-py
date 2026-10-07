"""Send and print UDP replies from a caller-selected Freebind source.

Run against one or more UDP echo peers, for example:
    python examples/udp.py --source 192.0.2.10 --peer 198.51.100.2 --port 9000
Add ``--peer`` again to send to another destination. ``--connected`` uses
send/recv on a single connected UDP peer; ``--asyncio`` uses loop datagrams.
"""

import argparse
import asyncio
import socket

from freebind import Source, new_socket


class _Replies(asyncio.DatagramProtocol):
    def __init__(self):
        self.received = asyncio.Queue()
        self.closed = asyncio.get_running_loop().create_future()

    def datagram_received(self, data, address):
        self.received.put_nowait((data, address))

    def connection_lost(self, error):
        if not self.closed.done():
            self.closed.set_result(error)


def _destinations(peers, port, family):
    return [
        socket.getaddrinfo(peer, port, family=family, type=socket.SOCK_DGRAM)[0][4]
        for peer in peers
    ]


def _send(args, peers, source):
    with new_socket(source, type=socket.SOCK_DGRAM) as sock:
        sock.settimeout(args.timeout)
        if args.connected:
            sock.connect(peers[0])
        for index, peer in enumerate(peers, 1):
            message = f"freebind-py UDP {index}".encode()
            if args.connected:
                sock.send(message)
                reply, address = sock.recvfrom(65535)
            else:
                sock.sendto(message, peer)
                reply, address = sock.recvfrom(65535)
            print(f"{address}: {reply.decode(errors='replace')}")


async def _send_async(args, peers, source):
    sock = new_socket(source, type=socket.SOCK_DGRAM)
    try:
        if args.connected:
            sock.connect(peers[0])
        transport, protocol = await asyncio.get_running_loop().create_datagram_endpoint(
            _Replies, sock=sock
        )
    except BaseException:
        sock.close()
        raise
    try:
        for index, peer in enumerate(peers, 1):
            transport.sendto(
                f"freebind-py UDP {index}".encode(),
                None if args.connected else peer,
            )
            reply, address = await asyncio.wait_for(
                protocol.received.get(), args.timeout
            )
            print(f"{address}: {reply.decode(errors='replace')}")
    finally:
        transport.close()
        await protocol.closed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="source IPv4/IPv6 address or CIDR")
    parser.add_argument("--peer", action="append", required=True, help="UDP echo peer; repeat for multiple destinations")
    parser.add_argument("--port", type=int, required=True, help="UDP peer port")
    parser.add_argument("--timeout", type=float, default=3.0)
    parser.add_argument("--connected", action="store_true", help="use connected UDP send/receive (requires one peer)")
    parser.add_argument("--asyncio", action="store_true", help="use asyncio datagram transport")
    args = parser.parse_args()
    if args.connected and len(args.peer) != 1:
        parser.error("--connected requires exactly one --peer")
    source = Source(args.source)
    family = next(iter(source.families))
    peers = _destinations(args.peer, args.port, family)
    if args.asyncio:
        asyncio.run(_send_async(args, peers, source))
    else:
        _send(args, peers, source)


if __name__ == "__main__":
    main()
