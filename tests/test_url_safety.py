"""P0-6: the SSRF guard used by feed sync and scrape preview (app/services/
ingestion.py) before any outbound fetch. Exercises the classifier directly
(app/services/url_safety.py) rather than through ingestion, since the interesting
cases here are IP-range classification and DNS-rebinding, not catalog parsing.
"""

import socket

import pytest

from app.services.url_safety import UnsafeURLError, assert_url_is_safe


def test_public_https_url_is_allowed() -> None:
    assert_url_is_safe("https://vendor.example.com/feed.json")


@pytest.mark.parametrize(
    "scheme_url", ["ftp://vendor.example.com/feed.json", "file:///etc/passwd"]
)
def test_non_http_schemes_are_rejected(scheme_url: str) -> None:
    with pytest.raises(UnsafeURLError):
        assert_url_is_safe(scheme_url)


def test_url_with_no_host_is_rejected() -> None:
    with pytest.raises(UnsafeURLError):
        assert_url_is_safe("https:///feed.json")


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",  # loopback
        "169.254.169.254",  # cloud metadata endpoint
        "10.0.0.5",  # RFC1918 private
        "172.16.0.5",  # RFC1918 private
        "192.168.1.1",  # RFC1918 private
        "0.0.0.0",  # unspecified
        "224.0.0.1",  # multicast
    ],
)
def test_literal_internal_ip_urls_are_rejected(ip: str) -> None:
    with pytest.raises(UnsafeURLError):
        assert_url_is_safe(f"http://{ip}/feed.json")


def test_hostname_that_resolves_to_a_private_ip_is_rejected_dns_rebinding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hostname that looks innocuous but currently resolves to an internal
    address must be rejected — safety is decided by resolution, not spelling."""

    def fake_getaddrinfo(host, *_args, **_kwargs):
        assert host == "attacker-controlled.example.com"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    with pytest.raises(UnsafeURLError):
        assert_url_is_safe("https://attacker-controlled.example.com/feed.json")


def test_unresolvable_host_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_getaddrinfo(host, *_args, **_kwargs):
        raise socket.gaierror("Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    with pytest.raises(UnsafeURLError):
        assert_url_is_safe("https://does-not-resolve.example.com/feed.json")
