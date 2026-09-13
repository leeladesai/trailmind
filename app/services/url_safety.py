"""SSRF guard for any outbound fetch triggered by tenant/admin-supplied input
(feed sync ING-1, scrape preview ING-2) — the server must never be usable as a
proxy to reach internal/private network targets (localhost, cloud metadata
endpoints such as 169.254.169.254, RFC1918 ranges, etc).

Validates both the URL's scheme and every IP address its hostname actually
resolves to. Checking the hostname string alone isn't enough — an attacker-
controlled domain can simply be pointed at a private IP (DNS rebinding), so
safety is decided by where the name currently resolves, not what it's named.
"""

import ipaddress
import socket
from urllib.parse import urlparse

ALLOWED_SCHEMES = {"http", "https"}


class UnsafeURLError(ValueError):
    """Raised when a URL is not safe for the server to fetch (SSRF guard)."""


def _is_public(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    return not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def assert_url_is_safe(url: str) -> None:
    """Raises UnsafeURLError if `url` must not be fetched from the server."""
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise UnsafeURLError(
            f"URL scheme must be http or https, got {parsed.scheme!r}."
        )
    host = parsed.hostname
    if not host:
        raise UnsafeURLError("URL has no host.")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise UnsafeURLError(f"Could not resolve host {host!r}: {exc}") from exc
    resolved_ips = {info[4][0] for info in infos}
    for ip in resolved_ips:
        if not _is_public(ip):
            raise UnsafeURLError(
                f"Refusing to fetch {url!r}: host {host!r} resolves to "
                f"non-public address {ip!r}."
            )
