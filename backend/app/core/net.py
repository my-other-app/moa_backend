"""Who is on the other end of a request.

In production nginx on the host reaches this container through Docker's
published port, so the connection's own address is Docker's bridge gateway
for every caller. nginx passes the real one on: it sets X-Real-IP and
appends the address it saw to X-Forwarded-For.

Those headers are only believed when the connection itself comes from a
private or loopback address -- nginx, a load balancer inside the VPC, a test
client. A request that reaches the published port straight from the
internet arrives from a public address, and whatever it says in its headers
is ignored, so it cannot claim to be someone else.

Inside X-Forwarded-For the trustworthy end is the right: each proxy appends
what it saw, and anything a client wrote itself sits to the left. The client
is the first public address reading from the right.

And the other way round: before the server calls an address someone gave it
(a webhook endpoint, a picture to fetch), `check_https_url` and
`resolve_public` make sure it is https and every address its host resolves to
is public -- never the server's own network, the cloud's metadata service, a
private range or loopback -- so the address cannot be used to reach what only
the server can. The caller then connects to one of the addresses resolved,
not the name again, so a DNS answer that changes in between (rebinding)
cannot slip a private address in.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import Optional
from urllib.parse import SplitResult, urlsplit


def _ip(text: str):
    try:
        return ipaddress.ip_address(text.strip())
    except ValueError:
        return None


def _internal(address) -> bool:
    return address.is_private or address.is_loopback or address.is_link_local


def real_client(peer: Optional[str], forwarded: Optional[str] = None, real_ip: Optional[str] = None) -> str:
    """The caller's address from the connection's own (`peer`) and the proxy
    headers, believing the headers only when the connection is internal."""
    peer = (peer or "").strip()
    peer_ip = _ip(peer)
    if peer_ip is None or not _internal(peer_ip):
        return peer
    hops = [h.strip() for h in (forwarded or "").split(",") if h.strip()]
    for hop in reversed(hops):
        address = _ip(hop)
        if address is not None and not _internal(address):
            return str(address)
    if real_ip and _ip(real_ip) is not None:
        return real_ip.strip()
    for hop in hops:
        if _ip(hop) is not None:
            return hop
    return peer


def client_address(request) -> str:
    """real_client for a request (or anything with .client and .headers)."""
    if request is None:
        return ""
    client = getattr(request, "client", None)
    headers = getattr(request, "headers", None) or {}
    return real_client(
        getattr(client, "host", None) if client else None,
        headers.get("x-forwarded-for"),
        headers.get("x-real-ip"),
    )


class ClientAddressMiddleware:
    """Puts the real caller into the ASGI scope's `client`, so everything
    after it -- the rate limiter, sign-in records, sessions, audit -- sees the
    person rather than the proxy. Added outermost (last) in doth.py."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") in ("http", "websocket") and scope.get("client"):
            host, port = scope["client"]
            headers = {}
            for name, value in scope.get("headers") or ():
                if name in (b"x-forwarded-for", b"x-real-ip"):
                    headers[name.decode("latin-1")] = value.decode("latin-1")
            real = real_client(host, headers.get("x-forwarded-for"), headers.get("x-real-ip"))
            if real and real != host:
                scope = dict(scope, client=(real, port))
        await self.app(scope, receive, send)


# ─── Calling an address someone gave us ──────────────────────────────────


class UnsafeURL(ValueError):
    """An address the server will not call: not https, or not public. The
    message says why, in words a club can act on."""


#: Names that are never on the public internet, whatever DNS says.
_LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa", ".intranet", ".corp")


def public_ip(text: str) -> bool:
    """A public unicast address: not private, loopback, link-local (the cloud
    metadata service), shared (100.64/10), reserved, multicast or unspecified.
    An IPv4 address written as IPv6 (::ffff:10.0.0.1) is judged as itself."""
    address = _ip(text) if isinstance(text, str) else None
    if address is None:
        return False
    if address.version == 6 and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return bool(address.is_global) and not address.is_multicast


def check_https_url(url: str, *, max_length: int = 2000) -> SplitResult:
    """The address's parts when the server may call it: https, a host, no
    user name or password, no fragment, not a local name, and -- when the
    host is written as an address -- a public one. Raises UnsafeURL."""
    text = (url or "").strip()
    if not text or len(text) > max_length:
        raise UnsafeURL("Give a full https:// address.")
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        raise UnsafeURL("That is not a valid address.")
    if parts.scheme != "https":
        raise UnsafeURL("The address must start with https:// (plain http is not allowed).")
    host = (parts.hostname or "").rstrip(".")
    if not host:
        raise UnsafeURL("The address has no host name.")
    if parts.username or parts.password:
        raise UnsafeURL("The address must not carry a user name or password.")
    if parts.fragment:
        raise UnsafeURL("The address must not have a #fragment.")
    if port == 0:
        raise UnsafeURL("That port cannot be used.")
    if _ip(host) is not None:
        if not public_ip(host):
            raise UnsafeURL("The address points at a private or local network.")
    elif host == "localhost" or host.endswith(_LOCAL_SUFFIXES) or "." not in host:
        raise UnsafeURL("The address points at a private or local network.")
    return parts


async def resolve_public(host: str, port: int) -> list[str]:
    """Every address `host` resolves to, when all of them are public (one
    private answer among public ones refuses the lot). Raises UnsafeURL."""
    host = (host or "").rstrip(".")
    if _ip(host) is not None:
        if not public_ip(host):
            raise UnsafeURL("The address points at a private or local network.")
        return [str(_ip(host))]
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM)
    except OSError:
        raise UnsafeURL(f"The host name {host} does not resolve.")
    addresses = sorted({info[4][0] for info in infos if info and info[4]})
    if not addresses:
        raise UnsafeURL(f"The host name {host} does not resolve.")
    if not all(public_ip(a) for a in addresses):
        raise UnsafeURL(f"{host} resolves to a private or local network address.")
    return addresses
