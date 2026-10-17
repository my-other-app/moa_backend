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
"""
from __future__ import annotations

import ipaddress
from typing import Optional


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
