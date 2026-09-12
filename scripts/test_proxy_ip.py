#!/usr/bin/env python3
"""Offline regressions for trusted proxy parsing and runtime updates."""

import runpy
import unittest
from pathlib import Path


HELPERS = runpy.run_path(str(Path(__file__).resolve().parents[1] / "bot/func_helper/proxy_ip.py"))
validate_proxy_cidrs = HELPERS["validate_proxy_cidrs"]
resolve_client_ip = HELPERS["resolve_client_ip"]


class ProxyIPTests(unittest.TestCase):
    def test_normalize_addresses_networks_and_duplicates(self):
        self.assertEqual(validate_proxy_cidrs([
            "198.51.100.20", "198.51.100.20/32", "2001:DB8::1", "192.0.2.25/24",
        ]), ["198.51.100.20/32", "2001:db8::1/128", "192.0.2.0/24"])

    def test_normalize_mapped_ipv4_networks(self):
        self.assertEqual(validate_proxy_cidrs(["::ffff:192.0.2.1", "::ffff:192.0.2.1/120"]),
                         ["192.0.2.1/32", "192.0.2.0/24"])

    def test_reject_unsafe_or_invalid_networks(self):
        for entry in ("0.0.0.0/0", "::/0", "::ffff:0:0/96", "invalid", "1.2.3.4\n",
                      "1.2.3.4\t", "1.2.3.4\x00", "fe80::1%eth0", "localhost", "1.2.3.4:80",
                      "1.2.3.4,2.3.4.5", 123, None, [], "x" * 81):
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                validate_proxy_cidrs([entry])

    def test_reject_nonlist_and_oversized_configuration(self):
        for values in (None, "192.0.2.1", {"192.0.2.1"}, ["192.0.2.1"] * 257,
                       [f"192.0.2.{index}" for index in range(129)]):
            with self.subTest(values=values), self.assertRaises(ValueError):
                validate_proxy_cidrs(values)

    def test_deduplicate_before_limit_for_adding_existing_proxy(self):
        entries = [f"192.0.2.{index}" for index in range(128)]
        self.assertEqual(len(validate_proxy_cidrs(entries + [entries[0]])), 128)

    def test_empty_configuration_ignores_forged_forwarded_ip(self):
        self.assertEqual(resolve_client_ip("198.51.100.20", "192.0.2.1", []), "198.51.100.20")

    def test_untrusted_peer_ignores_even_valid_forwarded_chain(self):
        self.assertEqual(resolve_client_ip("198.51.100.20", "192.0.2.1, 203.0.113.8", ["203.0.113.8"]),
                         "198.51.100.20")

    def test_trusted_peer_uses_forwarded_client(self):
        self.assertEqual(resolve_client_ip("198.51.100.20", "192.0.2.1", ["198.51.100.0/24"]), "192.0.2.1")

    def test_forged_left_prefix_stops_at_first_untrusted_hop(self):
        self.assertEqual(resolve_client_ip("198.51.100.20", "192.0.2.99, 203.0.113.10, 198.51.100.21",
                                          ["198.51.100.0/24"]), "203.0.113.10")

    def test_all_trusted_hops_use_leftmost_ip(self):
        self.assertEqual(resolve_client_ip("198.51.100.20", "198.51.100.22, 198.51.100.21",
                                          ["198.51.100.0/24"]), "198.51.100.22")

    def test_mixed_ipv4_ipv6_chain_and_networks(self):
        self.assertEqual(resolve_client_ip("2001:db8::1", "192.0.2.1, 198.51.100.21",
                                          ["2001:db8::/64", "198.51.100.0/24"]), "192.0.2.1")

    def test_mapped_ipv4_is_consistent_for_peer_and_chain(self):
        self.assertEqual(resolve_client_ip("::ffff:198.51.100.20", "::ffff:192.0.2.1",
                                          ["::ffff:198.51.100.0/120"]), "192.0.2.1")

    def test_invalid_chains_fall_back_to_actual_peer(self):
        for chain in (None, "", "192.0.2.1,", "unknown, 192.0.2.1", "192.0.2.1\n", "192.0.2.1:80",
                      "[2001:db8::1]", "fe80::1%eth0", "x" * 2049, ",".join(["192.0.2.1"] * 33)):
            with self.subTest(chain=chain):
                self.assertEqual(resolve_client_ip("198.51.100.20", chain, ["198.51.100.20"]), "198.51.100.20")

    def test_invalid_configuration_fails_closed(self):
        for config in (None, "198.51.100.20", ["0.0.0.0/0"], ["198.51.100.20", "bad"], [[]],
                       ["198.51.100.20"] * 129):
            with self.subTest(config=config):
                self.assertEqual(resolve_client_ip("198.51.100.20", "192.0.2.1", config), "198.51.100.20")

    def test_invalid_peers_raise(self):
        for peer in (None, "", "unknown", "127.0.0.1:8096", "127.0.0.1\n", "fe80::1%eth0"):
            with self.subTest(peer=peer), self.assertRaises(ValueError):
                resolve_client_ip(peer, "192.0.2.1", [])

    def test_runtime_add_remove_and_in_place_update(self):
        proxies = []
        self.assertEqual(resolve_client_ip("198.51.100.20", "192.0.2.1", proxies), "198.51.100.20")
        proxies.append("198.51.100.20")
        self.assertEqual(resolve_client_ip("198.51.100.20", "192.0.2.1", proxies), "192.0.2.1")
        proxies[0] = "203.0.113.1"
        self.assertEqual(resolve_client_ip("198.51.100.20", "192.0.2.1", proxies), "198.51.100.20")
        proxies.clear()
        self.assertEqual(resolve_client_ip("198.51.100.20", "192.0.2.1", proxies), "198.51.100.20")


if __name__ == "__main__":
    unittest.main()
