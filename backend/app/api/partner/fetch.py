"""Fetching a poster or a logo from the URL a partner gives, safely.

https on the default port only; every address the host resolves to must be
public (never the server's own network); no redirects; at most 8 MB, within
10 seconds; an image. The bytes are then stored by the picture columns as an
upload from the dashboard would be. Swapped in tests (`fetch_image`).
"""
from __future__ import annotations

import asyncio
import io
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from app.api.partner.errors import PartnerError

MAX_BYTES = 8 * 1024 * 1024
TIMEOUT = 10.0
IMAGE_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}


@dataclass
class Fetched:
    """Enough of an UploadFile for the services that save pictures."""

    filename: str
    content: bytes
    content_type: str

    async def read(self) -> bytes:
        return self.content

    async def seek(self, _pos: int) -> None:
        return None

    @property
    def file(self):
        return io.BytesIO(self.content)


def _refuse(field: str, message: str) -> PartnerError:
    return PartnerError("VALIDATION", message, details={"fields": {field: message}})


def _public(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return ip.is_global and not ip.is_multicast


async def fetch_image(url: str, field: str) -> Fetched:
    parts = urlsplit((url or "").strip())
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.port not in (None, 443):
        raise _refuse(field, f"{field} must be an https URL on the default port.")
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, parts.hostname, 443, type=socket.SOCK_STREAM)
    except OSError:
        raise _refuse(field, f"{field}'s host does not resolve.")
    if not infos or not all(_public(info[4][0]) for info in infos):
        raise _refuse(field, f"{field} is not on a public address.")
    body = b""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False) as http:
            async with http.stream("GET", url, headers={"Accept": "image/*", "User-Agent": "MyOtherApp-Integrations/1.0"}) as response:
                if response.status_code != 200:
                    raise _refuse(field, f"{field} answered {response.status_code}.")
                kind = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
                if kind not in IMAGE_TYPES:
                    raise _refuse(field, f"{field} is not a JPEG, PNG, WebP or GIF image.")
                async for chunk in response.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_BYTES:
                        raise _refuse(field, f"{field} is larger than 8 MB.")
    except httpx.HTTPError:
        raise _refuse(field, f"{field} could not be read.")
    if not body:
        raise _refuse(field, f"{field} is empty.")
    return Fetched(filename=f"{field}.{IMAGE_TYPES[kind]}", content=body, content_type=kind)
