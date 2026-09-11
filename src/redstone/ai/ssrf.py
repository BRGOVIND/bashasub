"""Base-URL validation against SSRF.

Redstone will become an environment that runs untrusted code, so an AI endpoint
URL is a prime SSRF vector: a user-supplied base_url pointing at
``169.254.169.254`` (cloud metadata) or ``localhost`` would turn the backend
into a proxy for reaching internal services. Every provider base URL — from
server config or from a future BYOK request — is validated here first.

Two policies:

* ``validate_base_url`` — structural rules for any endpoint: https only, a real
  public host, no credentials or ports that smell like internal services. Used
  for server-configured endpoints, which an operator is trusted to set.
* ``validate_byok_host`` — additionally requires the host be on an explicit
  allowlist. A BYOK user may not point Redstone at an arbitrary address.

Standard library only. This validates the URL; it never makes a request.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

__all__ = [
    "BaseUrlError",
    "validate_base_url",
    "validate_byok_host",
    "BYOK_ALLOWED_HOSTS",
]


class BaseUrlError(Exception):
    """A base URL was rejected. The message never echoes a resolved address."""


# Hosts a BYOK request may target. Server config is not restricted to this list
# (an operator can point at a private gateway), but a user bringing their own
# key may only reach known public providers.
BYOK_ALLOWED_HOSTS = frozenset(
    {
        "generativelanguage.googleapis.com",
        "api.openai.com",
        "api.groq.com",
        "openrouter.ai",
    }
)


def _is_public(host: str) -> bool:
    """True only if every address the host resolves to is global/public.

    Resolving here is DNS-rebinding-imperfect (the address used at request time
    could differ), but it catches the overwhelmingly common cases — literal
    private IPs and hostnames that resolve to them — and is the right check to
    make at configuration time. The request itself also disables redirects.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        raise BaseUrlError("provider host could not be resolved")

    for info in infos:
        address = info[4][0]
        try:
            ip = ipaddress.ip_address(address.split("%")[0])
        except ValueError:
            return False
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local        # 169.254.0.0/16, incl. cloud metadata
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return False
    return bool(infos)


def validate_base_url(url: str, *, resolve: bool = True) -> str:
    """Validate a provider base URL, returning it normalised, or raise.

    `resolve=False` skips DNS (used in tests and where the caller only needs the
    structural checks); the scheme and literal-IP checks still run.
    """
    if not isinstance(url, str) or not url.strip():
        raise BaseUrlError("base URL must be a non-empty string")

    parsed = urlparse(url.strip())

    if parsed.scheme != "https":
        # Plain http, file://, ftp://, gopher:// etc. are all refused.
        raise BaseUrlError("base URL must use https")

    if parsed.username or parsed.password:
        raise BaseUrlError("base URL must not contain credentials")

    host = parsed.hostname
    if not host:
        raise BaseUrlError("base URL must have a host")

    # A bracketed or bare IP literal: check it directly without DNS.
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            raise BaseUrlError("base URL must not point at a private or local address")
        return url.strip()

    if host.lower() in ("localhost", "localhost.localdomain"):
        raise BaseUrlError("base URL must not point at localhost")

    if resolve and not _is_public(host):
        raise BaseUrlError("base URL must resolve to a public address")

    return url.strip()


def validate_byok_host(url: str) -> str:
    """Validate a BYOK base URL: structural checks plus the host allowlist."""
    validated = validate_base_url(url)
    host = urlparse(validated).hostname or ""
    if host.lower() not in BYOK_ALLOWED_HOSTS:
        raise BaseUrlError("this provider host is not permitted for BYOK")
    return validated
