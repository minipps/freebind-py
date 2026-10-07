import errno
import ipaddress
import socket
import unittest
from unittest.mock import patch

from freebind._source import FamilyMismatchError, Source, random_ip


class RandomIPTests(unittest.TestCase):
    def test_ipv4_zero_maximum_and_intermediate_draws(self):
        with patch("freebind._source.secrets.randbits", return_value=0):
            self.assertEqual(random_ip("192.0.2.9/24"), "192.0.2.0")
        with patch("freebind._source.secrets.randbits", return_value=255):
            self.assertEqual(random_ip("192.0.2.0/24"), "192.0.2.255")
        with patch("freebind._source.secrets.randbits", return_value=42):
            self.assertEqual(random_ip("192.0.2.0/24"), "192.0.2.42")

    def test_ipv6_partial_and_non_byte_aligned_prefixes(self):
        with patch("freebind._source.secrets.randbits", return_value=0x2C):
            self.assertEqual(random_ip("2001:db8:1234::/48", bits=8), "2001:db8:1234:2c00::")
        with patch("freebind._source.secrets.randbits", return_value=0b101):
            self.assertEqual(random_ip("2001:db8::/61", bits=3), "2001:db8:0:5::")

    def test_edge_prefix_lengths_and_ipv6_zero_prefix(self):
        cases = (
            ("192.0.2.0/31", "192.0.2.1"),
            ("192.0.2.3/32", "192.0.2.3"),
            ("2001:db8::/63", "2001:db8::1"),
            ("2001:db8::/65", "2001:db8::1"),
            ("2001:db8::/127", "2001:db8::1"),
            ("2001:db8::1/128", "2001:db8::1"),
            ("::/0", "::1"),
        )
        for cidr, expected in cases:
            with self.subTest(cidr=cidr), patch(
                "freebind._source.secrets.randbits", return_value=1
            ):
                self.assertEqual(random_ip(cidr), expected)

    def test_singleton_and_zero_prefix(self):
        with patch("freebind._source.secrets.randbits", return_value=0):
            self.assertEqual(random_ip("192.0.2.7"), "192.0.2.7")
            self.assertEqual(random_ip("0.0.0.0/0", bits=0), "0.0.0.0")
        with patch("freebind._source.secrets.randbits", return_value=(1 << 128) - 1):
            self.assertEqual(random_ip("::/0"), "ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff")

    def test_bits_validation(self):
        for bits in (-1, 9):
            with self.subTest(bits=bits), self.assertRaises(ValueError):
                random_ip("192.0.2.0/24", bits=bits)
        for bits in (True, 1.5, "2"):
            with self.subTest(bits=bits), self.assertRaises(TypeError):
                random_ip("192.0.2.0/24", bits=bits)

    def test_scoped_input_is_rejected(self):
        with self.assertRaises(ValueError):
            random_ip("fe80::1%eth0/64")


class SourceTests(unittest.TestCase):
    def test_families_are_grouped_and_immutable(self):
        source = Source(("192.0.2.1", "2001:db8::1"))
        self.assertEqual(source.families, frozenset((socket.AF_INET, socket.AF_INET6)))
        self.assertIsInstance(source.families, frozenset)
        with patch("freebind._source.secrets.randbits", return_value=0):
            self.assertEqual(source.select(socket.AF_INET), "192.0.2.1")
            self.assertEqual(source.select(socket.AF_INET6), "2001:db8::1")

    def test_strict_and_non_strict_family_mismatch(self):
        source = Source("192.0.2.1")
        with self.assertRaises(FamilyMismatchError) as caught:
            source.select(socket.AF_INET6)
        self.assertEqual(caught.exception.errno, errno.EAFNOSUPPORT)
        self.assertEqual(FamilyMismatchError().errno, errno.EAFNOSUPPORT)
        self.assertIsNone(Source("192.0.2.1", strict=False).select(socket.AF_INET6))

    def test_networks_are_normalized_and_repeated_entries_are_preserved(self):
        source = Source(
            ("192.0.2.7/24", "198.51.100.0/24", "192.0.2.0/24"), bits=0
        )
        with patch(
            "freebind._source.secrets.choice",
            return_value=ipaddress.ip_network("192.0.2.0/24"),
        ) as choice:
            self.assertEqual(source.select(socket.AF_INET), "192.0.2.0")
        self.assertEqual(
            choice.call_args.args[0],
            (
                ipaddress.ip_network("192.0.2.0/24"),
                ipaddress.ip_network("198.51.100.0/24"),
                ipaddress.ip_network("192.0.2.0/24"),
            ),
        )

    def test_random_policy_draws_per_selection(self):
        source = Source("192.0.2.0/24")
        with patch("freebind._source.secrets.randbits", side_effect=(1, 2)):
            self.assertEqual(source.select(socket.AF_INET), "192.0.2.1")
            self.assertEqual(source.select(socket.AF_INET), "192.0.2.2")

    def test_sticky_policy_selects_once_per_family_at_construction(self):
        source_prefixes = ("192.0.2.0/24", "2001:db8::/120")
        with (
            patch("freebind._source.secrets.choice", side_effect=lambda options: options[0]),
            patch("freebind._source.secrets.randbits", side_effect=(1, 2)) as draw,
        ):
            source = Source(source_prefixes, mode="sticky")
            self.assertEqual(source.select(socket.AF_INET), "192.0.2.1")
            self.assertEqual(source.select(socket.AF_INET), "192.0.2.1")
            self.assertEqual(source.select(socket.AF_INET6), "2001:db8::2")
            self.assertEqual(source.select(socket.AF_INET6), "2001:db8::2")
        self.assertEqual(draw.call_count, 2)

    def test_configuration_validation_and_local_prefixes(self):
        for prefix in (
            "fe80::1%eth0/64",
            "fe80::/10",
            "fe80::1/0",
            "169.254.1.1",
            "::ffff:192.0.2.1",
            "::ffff:192.0.2.1/80",
            "::ffff:192.0.2.1/0",
        ):
            with self.subTest(prefix=prefix), self.assertRaises(ValueError):
                Source(prefix)
        self.assertEqual(Source("::/0").families, frozenset((socket.AF_INET6,)))
        with self.assertRaises(ValueError):
            Source(())
        with self.assertRaises(ValueError):
            Source("192.0.2.1", mode="fixed")
        with self.assertRaises(ValueError):
            Source(("192.0.2.0/24", "2001:db8::/64"), bits=9)
        with self.assertRaises(TypeError):
            Source("192.0.2.1", strict=1)


if __name__ == "__main__":
    unittest.main()
