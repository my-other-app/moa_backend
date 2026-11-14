"""Shared set-up for the profiles and Network tests (not a test module)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.api.profiles.models import NetworkPeople
from app.api.users.models import UserProfiles
from tests.conftest import auth_header

API = "/api/v1"
NOW = datetime.now(timezone.utc)


async def person(session, make_user, mint_token, handle: str, name: str, **profile):
    """An app user with a profile, their handle the username; (user, headers)."""
    user = await make_user(email=f"{handle}@example.com", username=handle)
    user.full_name = name
    user.phone = profile.pop("phone", None)
    session.add(UserProfiles(user_id=user.id, full_name=name, **profile))
    await session.flush()
    return user, auth_header(await mint_token(user))


async def profile_row(session, user) -> UserProfiles:
    return await session.scalar(select(UserProfiles).where(UserProfiles.user_id == user.id).execution_options(populate_existing=True))


async def row(session, owner, person_) -> NetworkPeople | None:
    return await session.scalar(
        select(NetworkPeople).where(NetworkPeople.owner_id == owner.id, NetworkPeople.person_id == person_.id).execution_options(populate_existing=True)
    )


async def save(client, headers, *, handle=None, code=None, method="search", **extra):
    body = {"method": method, **extra}
    if handle is not None:
        body["handle"] = handle
    if code is not None:
        body["code"] = code
    return await client.post(f"{API}/network", json=body, headers=headers)


async def live_event(make_club, make_event, make_registration, *guests, name="DevFest", starts=None, duration=4.0, email="net-host@example.com"):
    """An event on now (or from `starts`), each guest checked in."""
    owner, club = await make_club(name=f"{name} Club", email=email)
    event = await make_event(club=club, created_by=owner, name=name, event_datetime=starts or NOW - timedelta(hours=1), duration=duration)
    for g in guests:
        await make_registration(event=event, user=g, is_attended=True)
    return owner, club, event
