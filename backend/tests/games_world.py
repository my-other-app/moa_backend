"""A world for the games tests: a club, an event on, guests who are checked in,
and what it takes to sign them in to a game."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.api.checkins.models import CheckIns, CheckInVia
from app.api.games import common, tokens
from tests.conftest import auth_header

API = "/api/v1"


def games(event_id, path=""):
    return f"{API}/games/{event_id}{path}"


def gp(path=""):
    return f"{API}/games{path}"


def org(event_id, path=""):
    return f"{API}/events/{event_id}/games{path}"


def game(token):
    return {"Authorization": f"Game {token}"}


class World:
    def __init__(self, client, session, make_club, make_event, make_user, make_registration, mint_token):
        self.client, self.session = client, session
        self._make_club, self._make_event, self._make_user, self._make_registration, self._mint = make_club, make_event, make_user, make_registration, mint_token
        self.regs: list = []

    async def build(self, name="Games Night", **event):
        self.owner, self.club = await self._make_club(name=f"{name} Club", email=f"{uuid.uuid4().hex[:8]}@example.com")
        self.event = await self._make_event(club=self.club, created_by=self.owner, name=name, event_datetime=datetime.now(timezone.utc) - timedelta(hours=1), duration=6.0, **event)
        self.eid = self.event.id
        await self.session.commit()
        self.club_h = auth_header(await self._mint(self.owner))
        return self

    async def guest(self, name="Anjali Rao", checked_in=True):
        user = await self._make_user(email=f"{uuid.uuid4().hex[:10]}@example.com", username=uuid.uuid4().hex[:12])
        reg = await self._make_registration(event=self.event, user=user, full_name=name)
        if checked_in:
            self.session.add(CheckIns(event_id=self.eid, registration_id=reg.id, via=CheckInVia.gate, checked_in_at=datetime.now(timezone.utc)))
        # Ids are kept: a commit expires every row, and a test reads them outside a greenlet.
        reg.rid = reg.id
        await self.session.commit()
        self.regs.append(reg)
        return reg

    async def guests(self, n, prefix="Guest"):
        return [await self.guest(f"{prefix} {chr(65 + i % 26)}{i}") for i in range(n)]

    async def check_in(self, reg):
        self.session.add(CheckIns(event_id=self.eid, registration_id=reg.rid, via=CheckInVia.gate, checked_in_at=datetime.now(timezone.utc)))
        await self.session.commit()

    async def turn_on(self, **fields):
        r = await self.client.patch(org(self.event.id, "/settings"), json={"cards_on": True, "hunt_on": True, **fields}, headers=self.club_h)
        assert r.status_code == 200, r.text
        return r.json()

    async def player(self, reg):
        """The guest's game session: the header to send."""
        from app.api.events.models import EventRegistrationsLink, Events

        event = await self.session.get(Events, self.eid)
        reg = await self.session.get(EventRegistrationsLink, reg.rid)
        player = await common.ensure_player(self.session, self.eid, reg.id)
        out = await common.start_session(self.session, event, reg, player)
        return game(out["token"])

    def badge(self, reg, version=0):
        return tokens.badge(self.eid, reg.rid, version)


@pytest.fixture
def make_world(client, session, make_club, make_event, make_user, make_registration, mint_token, monkeypatch):
    from app.api.games import bus, cards, hunt
    from app.core.rate_limit import limiter

    monkeypatch.setattr(limiter, "enabled", False)
    # Nobody waits the real gaps between scans in a test.
    monkeypatch.setattr(cards, "COLLECT_GAP", 0.0)
    monkeypatch.setattr(hunt, "SCAN_GAP", 0.0)
    bus.reset_for_tests()

    async def build(name="Games Night", **event):
        return await World(client, session, make_club, make_event, make_user, make_registration, mint_token).build(name, **event)

    return build
