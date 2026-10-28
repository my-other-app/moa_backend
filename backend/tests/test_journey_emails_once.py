"""A burst of first registrations copies an event's journey emails once.

The scale study caught it: several first registrations at once each found
the event without its journey emails and each copied the club's four, so the
event held 12–24 and every attendee got each reminder three to six times.
These sessions run on their own connections and commit, as concurrent
requests do; the usual rolled-back test session can't show a race.
"""
import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.clubs.models import Clubs
from app.api.emails import service as emails
from app.api.emails.models import EmailTemplates
from app.api.events.models import EventCategories, Events
from app.api.users.models import Users, UserTypes
from app.db.core import SESSION_KWARGS
from tests.conftest import _make_user


async def test_a_burst_of_first_registrations_copies_the_journey_emails_once(_engine, monkeypatch):
    # Widen the gap between reading what the event has and copying what it
    # lacks, as a loaded server does, so the race shows every run.
    real_library = emails.ensure_library

    async def slow_library(session, club_id):
        rows = await real_library(session, club_id)
        await asyncio.sleep(0.2)
        return rows

    monkeypatch.setattr(emails, "ensure_library", slow_library)
    make = async_sessionmaker(bind=_engine, **SESSION_KWARGS)
    async with make() as s:
        owner = await _make_user(s, email="burst-owner@example.com", user_type=UserTypes.club, username="burstowner")
        club = Clubs(slug="burst-club-7q2", name="Burst Club", user_id=owner.id, is_verified=True)
        s.add(club)
        await s.flush()
        category = EventCategories(name="Burst", created_by_id=owner.id)
        s.add(category)
        await s.flush()
        event = Events(
            slug="burst-night-7q2", name="Burst Night", images=[], duration=2.0,
            event_datetime=datetime.now(timezone.utc) + timedelta(days=3),
            club_id=club.id, category_id=category.id,
        )
        s.add(event)
        await s.commit()
        owner_id, club_id, category_id, event_id = owner.id, club.id, category.id, event.id

    try:
        async def first_registration():
            async with make() as s:
                await emails.ensure_event_templates(s, await s.get(Events, event_id))

        await asyncio.gather(*(first_registration() for _ in range(6)))

        async with make() as s:
            on_event = await s.scalar(
                select(func.count()).select_from(EmailTemplates).where(EmailTemplates.event_id == event_id)
            )
            in_library = await s.scalar(
                select(func.count()).select_from(EmailTemplates)
                .where(EmailTemplates.club_id == club_id, EmailTemplates.event_id.is_(None))
            )
        assert on_event == len(emails.JOURNEY_STAGES)
        # The library: the four journey emails and the six review emails, once each.
        assert in_library == len(emails.LIBRARY_STAGES)
    finally:
        async with make() as s:
            await s.execute(delete(EmailTemplates).where(EmailTemplates.club_id == club_id))
            await s.execute(delete(Events).where(Events.id == event_id))
            await s.execute(delete(EventCategories).where(EventCategories.id == category_id))
            await s.execute(delete(Clubs).where(Clubs.id == club_id))
            await s.execute(delete(Users).where(Users.id == owner_id))
            await s.commit()
