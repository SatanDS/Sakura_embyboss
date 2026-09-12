"""Bounded client-address parsing for the CDN -> Caddy -> Emby chain."""

from functools import lru_cache
from ipaddress import IPv4Network, IPv6Network, ip_address, ip_network


MAX_PROXY_CIDRS = 128
MAX_FORWARDED_HOPS = 32
MAX_FORWARDED_LENGTH = 2048
_MAPPED_IPV4 = IPv6Network("::ffff:0:0/96")


def _has_controls(value):
    return any(ord(char) < 32 or ord(char) == 127 for char in value)


def _address(value):
    if not isinstance(value, str) or len(value) > 64 or _has_controls(value) or "%" in value:
        raise ValueError("Invalid proxy IP address")
    address = ip_address(value.strip())
    return getattr(address, "ipv4_mapped", None) or address


def validate_proxy_cidrs(values):
    """Return deduplicated canonical networks, or reject the entire input.

    Callers accepting administrator text must split spaces, commas and newlines
    before calling this function. Bare IPs become /32 or /128 networks.
    """
    if not isinstance(values, (list, tuple)) or len(values) > MAX_PROXY_CIDRS * 2:
        raise ValueError("Trusted proxies must be a list with at most 128 entries")
    canonical = []
    for value in values:
        if not isinstance(value, str) or len(value) > 80 or _has_controls(value) or "%" in value:
            raise ValueError("Invalid trusted proxy IP or network")
        try:
            network = ip_network(value.strip(), strict=False)
        except ValueError as exc:
            raise ValueError("Invalid trusted proxy IP or network") from exc
        if isinstance(network, IPv6Network) and network.subnet_of(_MAPPED_IPV4):
            network = IPv4Network((int(network.network_address.ipv4_mapped), network.prefixlen - 96))
        if network.prefixlen == 0:
            raise ValueError("Trusting every address is not allowed")
        rendered = str(network)
        if rendered not in canonical:
            canonical.append(rendered)
        if len(canonical) > MAX_PROXY_CIDRS:
            raise ValueError("Trusted proxies must be a list with at most 128 entries")
    return canonical


@lru_cache(maxsize=32)
def _trusted_networks(values):
    return tuple(ip_network(value) for value in validate_proxy_cidrs(values))


def resolve_client_ip(peer_ip, forwarded_for, trusted_cidrs):
    """Use XFF only behind a configured peer; malformed chains use that peer.

    Starting at the rightmost hop stops a client's forged prefix from replacing
    the first untrusted address. Invalid peer addresses raise ValueError.
    """
    peer = _address(peer_ip)
    fallback = str(peer)
    if not isinstance(trusted_cidrs, (list, tuple)) or len(trusted_cidrs) > MAX_PROXY_CIDRS:
        return fallback
    try:
        networks = _trusted_networks(tuple(trusted_cidrs))
    except (TypeError, ValueError):
        return fallback

    def trusted(address):
        return any(address.version == network.version and address in network for network in networks)

    if not trusted(peer):
        return fallback
    if not isinstance(forwarded_for, str) or not forwarded_for or len(forwarded_for) > MAX_FORWARDED_LENGTH:
        return fallback
    hops = forwarded_for.split(",")
    if len(hops) > MAX_FORWARDED_HOPS:
        return fallback
    try:
        addresses = [_address(hop) for hop in hops]
    except ValueError:
        return fallback
    current = peer
    for address in reversed(addresses):
        if not trusted(current):
            break
        current = address
    return str(current)
