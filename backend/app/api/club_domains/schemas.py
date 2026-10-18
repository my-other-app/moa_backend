from datetime import datetime

from pydantic import Field

from app.core.response.base_model import CustomBaseModel


class DomainIn(CustomBaseModel):
    #: As typed: `www.theirclub.org`, `https://www.theirclub.org/`, an IDN... normalised by the server.
    host: str = Field(..., min_length=1, max_length=300)


class DomainRecord(CustomBaseModel):
    #: CNAME or TXT.
    type: str
    name: str
    value: str
    #: route (the CNAME that brings the traffic), ownership (proves the domain
    #: is the club's) or ssl (validates the certificate).
    purpose: str


class DomainOut(CustomBaseModel):
    host: str
    #: pending | active | paused | failed.
    status: str
    records: list[DomainRecord] = Field(default_factory=list)
    #: Cloudflare's word for the certificate: pending_validation, active, ...
    ssl_status: str | None = None
    last_error: str | None = None
    verified_at: datetime | None = None
    created_at: datetime


class SiteDomainOut(CustomBaseModel):
    #: False when the server has no Cloudflare credentials: nothing can be connected.
    available: bool
    #: Whether the club's plan includes its own domain.
    entitled: bool
    #: `{slug}.myotherapp.in`; null for a slug that cannot be a subdomain.
    default_host: str | None = None
    domain: DomainOut | None = None


class ResolveOut(CustomBaseModel):
    club_slug: str
    #: active: serve the site here. paused: redirect to canonical_host.
    status: str
    #: Where the site lives: the club's own domain while it is active, else `{slug}.myotherapp.in`.
    canonical_host: str


class AdminDomainClub(CustomBaseModel):
    id: int
    name: str | None = None
    slug: str | None = None


class AdminDomainItem(CustomBaseModel):
    id: int
    club: AdminDomainClub | None = None
    host: str
    status: str
    ssl_status: str | None = None
    last_error: str | None = None
    verified_at: datetime | None = None
    created_at: datetime


class AdminDomainList(CustomBaseModel):
    items: list[AdminDomainItem]
    total: int
