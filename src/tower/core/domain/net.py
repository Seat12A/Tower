"""Network address rules (SSRF protection). Standard library only."""

from __future__ import annotations

import ipaddress

_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_NAT64 = ipaddress.ip_network("64:ff9b::/96")

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def is_public_ip(ip: str | IPAddress) -> bool:
    """True only for globally routable unicast addresses.

    Refuses private, loopback, link-local, multicast, reserved, unspecified, CGNAT
    (100.64/10) addresses, and IPv6 forms that embed an IPv4 address (mapped, NAT64,
    6to4, Teredo) when the embedded address is not public. Invalid input → False.
    """
    try:
        addr = ipaddress.ip_address(ip) if isinstance(ip, str) else ip
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address):
        embedded: ipaddress.IPv4Address | None = addr.ipv4_mapped or addr.sixtofour
        if embedded is None and addr.teredo is not None:
            embedded = addr.teredo[1]
        if embedded is None and addr in _NAT64:
            embedded = ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF)
        if embedded is not None:
            return is_public_ip(embedded)
    if isinstance(addr, ipaddress.IPv4Address) and addr in _CGNAT:
        return False
    return not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
        or not addr.is_global
    )
