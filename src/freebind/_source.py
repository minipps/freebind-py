"""Source address generation and policy selection."""

import errno
import ipaddress
import secrets
import socket
from collections.abc import Sequence


def _validate_bits(bits: int | None, host_bits: int) -> int:
    if bits is None:
        return host_bits
    if not isinstance(bits, int) or isinstance(bits, bool):
        raise TypeError("bits must be an integer or None")
    if not 0 <= bits <= host_bits:
        raise ValueError(f"bits must be between 0 and {host_bits}")
    return bits


def _random_ip(network: ipaddress.IPv4Network | ipaddress.IPv6Network, bits: int | None) -> str:
    host_bits = network.max_prefixlen - network.prefixlen
    randomized_bits = _validate_bits(bits, host_bits)
    value = int(network.network_address) | (
        secrets.randbits(randomized_bits) << (host_bits - randomized_bits)
    )
    return str(type(network.network_address)(value))


def random_ip(cidr: str, *, bits: int | None = None) -> str:
    """Choose an address within a CIDR, randomizing its first host bits."""
    if not isinstance(cidr, str):
        raise TypeError("cidr must be a string")
    if "%" in cidr:
        raise ValueError("scoped IPv6 addresses are not supported")
    network = ipaddress.ip_network(cidr, strict=False)
    return _random_ip(network, bits)


class FamilyMismatchError(OSError):
    """Raised when a strict source policy has no prefix for a family."""

    def __init__(self, message: str = "No source prefix for address family") -> None:
        super().__init__(errno.EAFNOSUPPORT, message)


class Source:
    """Select source addresses from one or more configured prefixes."""

    def __init__(
        self,
        prefixes: str | Sequence[str],
        *,
        mode: str = "random",
        bits: int | None = None,
        strict: bool = True,
        interface: str | None = None,
    ) -> None:
        if mode not in ("random", "sticky"):
            raise ValueError("mode must be 'random' or 'sticky'")
        if not isinstance(strict, bool):
            raise TypeError("strict must be a bool")
        if isinstance(prefixes, str):
            entries = (prefixes,)
        elif isinstance(prefixes, Sequence):
            entries = tuple(prefixes)
        else:
            raise TypeError("prefixes must be a string or sequence of strings")
        if not entries:
            raise ValueError("at least one source prefix is required")

        grouped: dict[int, list[ipaddress.IPv4Network | ipaddress.IPv6Network]] = {}
        for entry in entries:
            if not isinstance(entry, str):
                raise TypeError("source prefixes must be strings")
            if "%" in entry:
                raise ValueError("scoped IPv6 addresses are not supported")
            address = ipaddress.ip_interface(entry).ip
            if address.is_link_local or getattr(address, "ipv4_mapped", None) is not None:
                raise ValueError("link-local and IPv4-mapped source prefixes are not supported")
            network = ipaddress.ip_network(entry, strict=False)
            _validate_bits(bits, network.max_prefixlen - network.prefixlen)
            family = socket.AF_INET if network.version == 4 else socket.AF_INET6
            grouped.setdefault(family, []).append(network)

        self.mode = mode
        self.bits = bits
        self.strict = strict
        self.interface = interface
        self._prefixes = {family: tuple(networks) for family, networks in grouped.items()}
        self._families = frozenset(self._prefixes)
        self._sticky = (
            {family: self._draw(family) for family in self._prefixes}
            if mode == "sticky"
            else {}
        )

    @property
    def families(self) -> frozenset[int]:
        """Configured socket address families."""
        return self._families

    def _draw(self, family: int) -> str:
        return _random_ip(secrets.choice(self._prefixes[family]), self.bits)

    def select(self, family: int) -> str | None:
        if family not in self._prefixes:
            if not self.strict:
                return None
            raise FamilyMismatchError(
                f"No source prefix configured for address family {family}"
            )
        if self.mode == "sticky":
            return self._sticky[family]
        return self._draw(family)
