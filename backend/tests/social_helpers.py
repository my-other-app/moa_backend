"""What the social posting tests share: a club with a published event and a
poster, its team (admin, editor, door), the fake platforms, an in-memory
bucket, and connecting an account the way a club does (the real OAuth
round trip against the fake)."""
from __future__ import annotations

import importlib.util
import io
import pathlib
from datetime import datetime, time, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
import pytest_asyncio
from alembic.migration import MigrationContext
from alembic.operations import Operations
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.api.auth.schemas import AuthTokenData
from app.api.closeout.models import EventStatus
from app.api.club_access.models import ClubMembers
from app.api.social.models import SocialAccounts
from app.api.subscriptions.models import Plans
from app.core.auth.jwt import create_access_token
from app.core.rate_limit import limiter
from app.core.storage import bucket, objects
from tests.conftest import auth_header
from tests.fake_social import install

API = "/api/v1"
IST = timezone(timedelta(hours=5, minutes=30))
DASHBOARD = "https://club-dashboard.myotherapp.in/settings/social"


def jpeg(width: int = 1080, height: int = 1350, colour=(200, 40, 40)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(out, "JPEG")
    return out.getvalue()


def at_ist(days: int, hour: int, minute: int = 0) -> datetime:
    day = (datetime.now(IST) + timedelta(days=days)).date()
    return datetime.combine(day, time(hour, minute), tzinfo=IST).astimezone(timezone.utc)


@pytest.fixture
def files(monkeypatch):
    held: dict = {}
    monkeypatch.setattr(objects, "put_bytes", lambda key, data, content_type: held.__setitem__(key, bytes(data)))
    monkeypatch.setattr(objects, "get_bytes", lambda key: held.get(key))
    return held


@pytest.fixture
def fake(monkeypatch):
    return install(monkeypatch)


def member_token(owner, member) -> dict:
    token = create_access_token(data=AuthTokenData(user_id=owner.id, token_type="access_token", token_version=owner.token_version,
                                                   member_id=member.id).model_dump())
    return auth_header(token)


@pytest_asyncio.fixture
async def world(session, make_club, make_event, make_user, mint_token, files, fake, monkeypatch):
    monkeypatch.setattr(limiter, "enabled", False)
    owner, club = await make_club(name="Circuit Society", email="circuit@example.com")
    poster = f"{bucket.prefix('events', 'posters')}v-{'a' * 32}.jpg"
    files[poster] = jpeg()
    starts = at_ist(10, 18)
    event = await make_event(club=club, created_by=owner, name="Hack Night", event_datetime=starts, duration=3.0,
                             location_name="Main Hall", lifecycle=EventStatus.published,
                             about="Build something in one night with friends. Food and mentors on us.",
                             reg_enddate=starts - timedelta(days=1))
    event.poster = poster
    team = {}
    for role in ("admin", "editor", "door"):
        m = ClubMembers(club_id=club.id, email=f"{role}@circuit.example", full_name=f"{role.title()} Person", role=role, status="active")
        session.add(m)
        team[role] = m
    await session.flush()
    app_user = await make_user(email="editor@circuit.example")
    await session.commit()
    await session.refresh(owner)
    await session.refresh(event)
    return SimpleNamespace(
        owner=owner, club=club, event=event, poster=poster, fake=fake, files=files, team=team, app_user=app_user,
        h=auth_header(await mint_token(owner)),
        admin=member_token(owner, team["admin"]), editor=member_token(owner, team["editor"]), door=member_token(owner, team["door"]),
    )


def state_of(authorize_url: str) -> str:
    return parse_qs(urlsplit(authorize_url).query)["state"][0]


async def connect(client, w, platform: str, headers=None, code: str = "the-code") -> dict:
    """Connect an account as a club does: Connect, the platform's sign-in, the callback."""
    r = await client.post(f"{API}/clubs/social/accounts/{platform}/connect", json={"return_to": DASHBOARD}, headers=headers or w.h)
    assert r.status_code == 200, r.text
    state = state_of(r.json()["authorize_url"])
    back = await client.get(f"{API}/social/oauth/{platform}/callback", params={"code": code, "state": state})
    assert back.status_code == 302, back.text
    q = parse_qs(urlsplit(back.headers["location"]).query)
    return {k: v[0] for k, v in q.items()}


async def account(session, club_id: int, platform: str) -> SocialAccounts:
    return await session.scalar(select(SocialAccounts).where(SocialAccounts.club_id == club_id, SocialAccounts.platform == platform,
                                                             SocialAccounts.status != "disconnected"))


def _migration(name: str):
    path = pathlib.Path(__file__).resolve().parents[1] / "migrations" / "versions" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def seed_plans(session) -> dict:
    """The six plans production has, with this branch's grant of the social flags."""
    plans, social = _migration("club_plans"), _migration("social_posting")
    connection = await session.connection()

    def seed(sync):
        with Operations.context(MigrationContext.configure(connection=sync)):
            plans.seed_plans()
            social.grant()

    await connection.run_sync(seed)
    rows = (await session.execute(select(Plans).options(selectinload(Plans.limits), selectinload(Plans.features)))).scalars().all()
    return {p.key: p for p in rows}
