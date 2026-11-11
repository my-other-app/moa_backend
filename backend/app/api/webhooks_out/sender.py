"""One POST to an endpoint, guarded.

- https only, and every address the host resolves to must be public
  (app/core/net.py): never the server's own network, the cloud's metadata
  service, a private range or loopback. Checked at every send, not only when
  the endpoint was added -- a name can be pointed elsewhere later.
- The connection goes to the address that was checked, with the host name
  for TLS (SNI and the certificate) and the Host header, so a DNS answer
  that changes between the check and the connection cannot slip a private
  address in.
- No redirects are followed (a 3xx is a failure: the endpoint should answer
  itself), no proxy from the environment is used, 10 seconds at most, and
  only the first RESPONSE_BODY_MAX characters of the answer are kept.

Tests swap TRANSPORT for an httpx.MockTransport and RESOLVE for a stand-in.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlunsplit

import httpx

from app.core import net

TIMEOUT_SECONDS = 10.0
RESPONSE_BODY_MAX = 2000
USER_AGENT = "MyOtherApp-Webhooks/1.0 (+https://myotherapp.in/developers)"

#: The transport the client uses: None is httpx's own (a real connection).
TRANSPORT: Optional[httpx.AsyncBaseTransport] = None
#: (host, port) -> the public addresses; raises net.UnsafeURL.
RESOLVE = net.resolve_public


@dataclass
class Result:
    ok: bool
    status: Optional[int] = None
    body: Optional[str] = None
    error: Optional[str] = None
    duration_ms: int = 0
    #: The address was refused before anything was sent (not public, not https).
    refused: bool = False


def _netloc(ip: str, port: int) -> str:
    return f"[{ip}]:{port}" if ":" in ip else f"{ip}:{port}"


async def post(url: str, body: bytes, headers: dict[str, str], *, timeout: float = TIMEOUT_SECONDS) -> Result:
    started = time.monotonic()

    def took() -> int:
        return int((time.monotonic() - started) * 1000)

    try:
        parts = net.check_https_url(url)
        port = parts.port or 443
        addresses = await RESOLVE(parts.hostname, port)
    except net.UnsafeURL as exc:
        return Result(ok=False, error=f"Not sent: {exc}"[:300], duration_ms=took(), refused=True)
    host = parts.hostname
    pinned = urlunsplit((parts.scheme, _netloc(addresses[0], port), parts.path or "/", parts.query, ""))
    sent = dict(headers)
    sent["Host"] = host if port == 443 else f"{host}:{port}"
    sent.setdefault("User-Agent", USER_AGENT)
    sent.setdefault("Content-Type", "application/json")
    try:
        async with httpx.AsyncClient(transport=TRANSPORT, timeout=timeout, follow_redirects=False, trust_env=False) as client:
            request = client.build_request("POST", pinned, content=body, headers=sent, extensions={"sni_hostname": host})
            response = await client.send(request, stream=True)
            try:
                kept = b""
                async for chunk in response.aiter_bytes():
                    kept += chunk
                    if len(kept) >= RESPONSE_BODY_MAX * 4:
                        break
            finally:
                await response.aclose()
    except httpx.TimeoutException:
        return Result(ok=False, error=f"No answer within {timeout:g} seconds.", duration_ms=took())
    except httpx.HTTPError as exc:
        reason = str(exc) or exc.__class__.__name__
        return Result(ok=False, error=f"Could not connect: {reason}"[:300], duration_ms=took())
    text = kept.decode("utf-8", errors="replace")[:RESPONSE_BODY_MAX]
    ok = 200 <= response.status_code < 300
    error = None if ok else (f"Answered {response.status_code}" + (" (redirects are not followed)" if 300 <= response.status_code < 400 else ""))
    return Result(ok=ok, status=response.status_code, body=text, error=error, duration_ms=took())
