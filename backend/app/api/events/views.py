"""An event page's views: once a visitor a day, written in batches.

Every open of an event's page (the app's GET /events/info/{id}, the site's
/events/count-view/{id}) used to load the event with its club, add a row to
event_page_views and write page_views back as it had read it plus one: a
write on every read, and two readers at once counted one. Now a view is
recorded in this process (discover.visitors.ViewBuffer): one per visitor
(an account, or today's salted hash of address and browser) per event per
day, the club's own looks not at all, and a batch of them is one statement
that inserts the rows and moves each event's page_views by how many went in.
page_views therefore counts visitors a day rather than every load; it feeds
"popular", where a refresh button should not count.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select, text

from app.api.clubs.models import Clubs
from app.api.discover import visitors
from app.api.events.models import Events
from app.core.net import client_address
from app.response import CustomHTTPException


async def _write(session, items: list[tuple]) -> None:
    """(event id, user id, browser, address) per view: the rows of events
    that still exist go in, and page_views moves by how many did."""
    if not items:
        return
    await session.execute(
        text(
            """
            WITH v AS (
              SELECT * FROM unnest(CAST(:events AS integer[]), CAST(:users AS integer[]), CAST(:agents AS varchar[]), CAST(:ips AS varchar[]))
                AS v(event_id, user_id, user_agent, ip_address)
            ),
            added AS (
              INSERT INTO event_page_views (id, event_id, user_agent, ip_address, user_id, created_at, updated_at, is_deleted)
              SELECT gen_random_uuid(), v.event_id, v.user_agent, v.ip_address, v.user_id, now(), now(), false
              FROM v JOIN events e ON e.id = v.event_id
              RETURNING event_id
            ),
            counted AS (SELECT event_id, count(*) AS n FROM added GROUP BY event_id)
            UPDATE events e SET page_views = e.page_views + c.n FROM counted c WHERE e.id = c.event_id
            """
        ),
        {
            "events": [i[0] for i in items], "users": [i[1] for i in items],
            "agents": [i[2] for i in items], "ips": [i[3] for i in items],
        },
    )


VIEWS = visitors.ViewBuffer("event page", _write)


async def record(session, request, ref, user_id: Optional[int] = None) -> None:
    """Count a look at an event's page (`ref`: its id or slug). 404 for no such event."""
    is_id = isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit())
    cond = Events.id == int(ref) if is_id else Events.slug == ref
    if is_id and int(ref) >= 2**31:
        raise CustomHTTPException(404, message="Event not found")
    row = (await session.execute(select(Events.id, Clubs.user_id).outerjoin(Clubs, Clubs.id == Events.club_id).where(cond))).first()
    if row is None:
        raise CustomHTTPException(404, message="Event not found")
    event_id, owner_id = row
    if user_id and user_id == owner_id:
        return  # the club looking at its own page
    visitor = f"u:{user_id}" if user_id else f"a:{visitors.anonymous(request) or 'unknown'}"
    agent = ((request.headers.get("user-agent") or "") if request is not None else "")[:500] or None
    await VIEWS.record(session, [((event_id, visitor, visitors.today()), (event_id, user_id, agent, client_address(request) or None))])
    await session.commit()
