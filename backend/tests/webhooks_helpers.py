"""Outgoing webhooks in the suite: a signing key, no settle delay, the club
allocated, DNS answered by a stand-in, and every POST caught by an
httpx.MockTransport that answers as the test says. Nothing leaves the suite."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable, Optional

import httpx
import pytest
from cryptography.fernet import Fernet

PUBLIC_IP = "93.184.216.34"
PRIVATE_HOSTS = {"internal.example.com": "10.0.0.5", "metadata.example.com": "169.254.169.254"}


@dataclass
class Hub:
    """What the endpoints received, and how they answer."""

    requests: list = field(default_factory=list)
    answer: Callable[[httpx.Request], httpx.Response] = lambda request: httpx.Response(200, text="ok")
    resolved: dict = field(default_factory=dict)
    allocated: set = field(default_factory=set)
    #: (club_id, client_id) -> scopes of a partner's connection.
    partner_scopes: dict = field(default_factory=dict)

    def bodies(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests]

    def types(self) -> list[str]:
        return [b["type"] for b in self.bodies()]

    def reply(self, status: int, text: str = "") -> None:
        self.answer = lambda request: httpx.Response(status, text=text)

    def clear(self) -> None:
        self.requests.clear()


def _hub(monkeypatch) -> Hub:
    """A key, no settle delay, DNS and HTTP caught; the seams as installed."""
    from app.api.webhooks_out import sender
    from app.config import settings
    from app.core import net

    h = Hub()
    monkeypatch.setattr(settings, "WEBHOOK_KEYS_SECRET", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "WEBHOOKS_SETTLE_SECONDS", 0.0)

    async def resolve(host: str, port: int) -> list[str]:
        h.resolved[host] = port
        if host in PRIVATE_HOSTS:
            raise net.UnsafeURL(f"{host} resolves to a private or local network address.")
        if host.endswith(".invalid"):
            raise net.UnsafeURL(f"The host name {host} does not resolve.")
        return [PUBLIC_IP]

    def handle(request: httpx.Request) -> httpx.Response:
        h.requests.append(request)
        return h.answer(request)

    monkeypatch.setattr(sender, "RESOLVE", resolve)
    monkeypatch.setattr(sender, "TRANSPORT", httpx.MockTransport(handle))
    return h


@pytest.fixture
def real_hub(monkeypatch):
    """The partner API's real answers behind the seams (webhooks_out.wiring)."""
    import doth  # noqa: F401 - installs the seams, as the API does
    from app.api.webhooks_out import seams

    seams.reset()
    yield _hub(monkeypatch)
    seams.reset()


@pytest.fixture
def hub(monkeypatch):
    """Stand-ins behind the seams: the test says who is allocated and what a partner may read."""
    from app.api.webhooks_out import seams

    h = _hub(monkeypatch)

    async def allocated(session, club_id):
        return club_id in h.allocated

    async def scopes(session, club_id, partner_app_id, client_id, *, livemode=True):
        return h.partner_scopes.get((club_id, client_id), frozenset())

    async def contact(session, partner_app_id, client_id):
        return ("Acme Events", "ops@acme.example") if client_id == "partner_acme" else None

    async def external_id(session, client_id, object_type, object_id):
        return f"ext-{object_type}-{object_id}" if client_id == "partner_acme" else None

    seams.configure(club_allocated=allocated, partner_scopes=scopes, partner_contact=contact, external_id=external_id)
    yield h
    seams.reset()


async def add_endpoint(session, club_id: int, *, url: str = "https://hooks.example.com/moa", events=("*",), **fields):
    """An endpoint made straight through the service, as the dashboard does."""
    from app.api.webhooks_out import service

    owner = fields.pop("owner", None) or service.Owner(club_id=club_id, name="Test Owner")
    data = {"url": url, "events": list(events), **fields}
    endpoint, secret = await service.create(session, owner, data)
    await session.commit()
    return endpoint, secret


async def deliver_all(session) -> None:
    """Commit what the test wrote and run the jobs it set off (the fan-out, then the sends)."""
    from app.core import after_commit, jobs

    await session.commit()
    for _ in range(4):
        await jobs.settle()
    await after_commit.drain()


async def events_of(session, type_: Optional[str] = None) -> list:
    from sqlalchemy import select

    from app.api.webhooks_out.models import WebhookEvents

    stmt = select(WebhookEvents).order_by(WebhookEvents.seq)
    if type_:
        stmt = stmt.where(WebhookEvents.type == type_)
    return list(await session.scalars(stmt))
