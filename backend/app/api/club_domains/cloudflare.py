"""Cloudflare for SaaS: the custom hostnames on the myotherapp.in zone.

Four calls, over httpx's async client so nothing blocks the event loop:
create, get, delete and list, plus `refresh` (a PATCH with the same SSL
settings, which Cloudflare reads as "check the validation records now").

Every hostname is made with TXT validation for its certificate
(`ssl: {method: "txt", type: "dv"}`) and comes back with a TXT ownership
record (`ownership_verification`), so a club can add all three records
before pointing traffic at us. Certificates renew by HTTP on their own while
the CNAME is in place.

The token (CLOUDFLARE_API_TOKEN) needs Zone > SSL and Certificates > Edit on
the zone (CLOUDFLARE_ZONE_ID). Without both, `available()` is false and the
feature says so; nothing here is ever called. The token never appears in a
log line or an error.
"""
from __future__ import annotations

import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

API = "https://api.cloudflare.com/client/v4"
TIMEOUT = httpx.Timeout(15.0, connect=5.0)
#: The certificate: domain-validated, by a TXT record the club adds.
SSL = {"method": "txt", "type": "dv", "settings": {"min_tls_version": "1.2"}}
#: Cloudflare's code for "Duplicate custom hostname found."
DUPLICATE = 1406


class CloudflareError(Exception):
    """Cloudflare refused, or could not be reached. `str()` is Cloudflare's
    own short message (or ours, for a network failure); never the token."""

    def __init__(self, message: str, *, status: int | None = None, code: int | None = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


class NotFound(CloudflareError):
    """No such custom hostname (any more)."""


def available() -> bool:
    return bool(settings.CLOUDFLARE_API_TOKEN and settings.CLOUDFLARE_ZONE_ID)


#: The transport under the client: None is the network. Tests put a fake Cloudflare here.
TRANSPORT: httpx.AsyncBaseTransport | None = None


def _http() -> httpx.AsyncClient:
    """One client per call."""
    return httpx.AsyncClient(
        base_url=API,
        timeout=TIMEOUT,
        headers={"Authorization": f"Bearer {settings.CLOUDFLARE_API_TOKEN}", "Content-Type": "application/json"},
        transport=TRANSPORT,
    )


def _short(errors) -> tuple[str, int | None]:
    for err in errors or []:
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])[:200], err.get("code")
    return "Cloudflare refused the request.", None


async def _call(method: str, path: str, *, json: dict | None = None, params: dict | None = None):
    if not available():
        raise CloudflareError("Custom domains are not set up on this server.")
    url = f"/zones/{settings.CLOUDFLARE_ZONE_ID}/custom_hostnames{path}"
    try:
        async with _http() as http:
            response = await http.request(method, url, json=json, params=params)
    except httpx.HTTPError as exc:
        logger.warning("Cloudflare %s %s: %s", method, path or "/", type(exc).__name__)
        raise CloudflareError("Cloudflare could not be reached. Try again in a minute.") from exc
    try:
        body = response.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    if response.status_code < 400 and body.get("success", True):
        return body.get("result")
    message, code = _short(body.get("errors"))
    if response.status_code == 404:
        raise NotFound(message, status=404, code=code)
    logger.warning("Cloudflare %s %s refused: %s %s (%s)", method, path or "/", response.status_code, code, message)
    raise CloudflareError(message, status=response.status_code, code=code)


async def create(host: str) -> dict:
    return await _call("POST", "", json={"hostname": host, "ssl": SSL})


async def get(hostname_id: str) -> dict:
    return await _call("GET", f"/{hostname_id}")


async def refresh(hostname_id: str) -> dict:
    """Ask Cloudflare to check the validation records now rather than at its
    next retry (it backs off to every four hours)."""
    return await _call("PATCH", f"/{hostname_id}", json={"ssl": SSL})


async def delete(hostname_id: str) -> bool:
    """True when it was deleted, or was already gone."""
    try:
        await _call("DELETE", f"/{hostname_id}")
    except NotFound:
        pass
    return True


async def find(host: str) -> dict | None:
    """The custom hostname for exactly `host`, if the zone has one."""
    result = await _call("GET", "", params={"hostname": host, "per_page": 5})
    for row in result or []:
        if isinstance(row, dict) and (row.get("hostname") or "").lower() == host:
            return row
    return None


async def list_hostnames(page: int = 1, per_page: int = 50) -> list[dict]:
    return list(await _call("GET", "", params={"page": page, "per_page": per_page}) or [])


def is_duplicate(exc: CloudflareError) -> bool:
    return exc.code == DUPLICATE or "duplicate" in (exc.message or "").lower()


# ─── Reading what Cloudflare said ────────────────────────────────────────


def records(result: dict, host: str) -> list[dict]:
    """The records the club adds: the CNAME that routes the traffic, the TXT
    that proves the domain is theirs, and the TXT(s) the certificate is
    validated with -- the last two as Cloudflare last gave them (the
    certificate's appear a few seconds after the hostname is made, and go
    once it is issued)."""
    out = [{"type": "CNAME", "name": host, "value": settings.SITES_CNAME_TARGET, "purpose": "route"}]
    ownership = (result or {}).get("ownership_verification") or {}
    if ownership.get("name") and ownership.get("value"):
        out.append({"type": "TXT", "name": ownership["name"], "value": ownership["value"], "purpose": "ownership"})
    seen = set()
    for rec in ((result or {}).get("ssl") or {}).get("validation_records") or []:
        name, value = (rec or {}).get("txt_name"), (rec or {}).get("txt_value")
        if name and value and (name, value) not in seen:
            seen.add((name, value))
            out.append({"type": "TXT", "name": name, "value": value, "purpose": "ssl"})
    return out


def statuses(result: dict) -> tuple[str | None, str | None]:
    """(the hostname's status, its certificate's)."""
    result = result or {}
    return result.get("status"), (result.get("ssl") or {}).get("status")


def is_live(result: dict) -> bool:
    hostname, ssl = statuses(result)
    return hostname == "active" and ssl == "active"


def problem(result: dict) -> str | None:
    """What Cloudflare says is in the way, as one short sentence, or None."""
    result = result or {}
    found = [str(e) for e in result.get("verification_errors") or [] if e]
    found += [str(e.get("message")) for e in (result.get("ssl") or {}).get("validation_errors") or [] if isinstance(e, dict) and e.get("message")]
    if not found:
        return None
    return "; ".join(dict.fromkeys(found))[:300]
