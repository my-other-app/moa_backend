"""The Posters page's numbers (CONTRACT.md §10.5): counted by the database."""
from __future__ import annotations

from collections import Counter

from sqlalchemy import and_, func, select

from app.api.events.models import EventRegistrationsLink
from app.api.guest_posters import links as poster_links
from app.api.guest_posters.models import GuestPosters, PosterSettings
from app.api.links.models import TrackingClicks, TrackingLinks

TOP = 10


async def stats(session, event) -> dict:
    from app.api.approvals.models import RegistrationRequests, WITHDRAWN
    from app.api.deliveries.models import Deliveries, DeliveryKind, DeliveryStatus

    settings_row = await session.get(PosterSettings, event.id)
    sent = select(Deliveries.registration_id).where(
        Deliveries.event_id == event.id, Deliveries.kind == DeliveryKind.ticket, Deliveries.status == DeliveryStatus.sent,
    )
    eligible = await session.scalar(
        select(func.count()).select_from(EventRegistrationsLink).where(
            EventRegistrationsLink.event_id == event.id, EventRegistrationsLink.is_deleted == False,  # noqa: E712
            EventRegistrationsLink.ticket_revoked_at.is_(None), EventRegistrationsLink.id.in_(sent),
        )
    ) or 0
    made, with_photo, renders, downloads, shares, copies = (await session.execute(
        select(
            func.count().filter(GuestPosters.made_at.isnot(None)), func.count().filter(GuestPosters.photo_key.isnot(None)),
            func.coalesce(func.sum(GuestPosters.render_count), 0), func.coalesce(func.sum(GuestPosters.downloads), 0),
            func.coalesce(func.sum(GuestPosters.shares), 0), func.coalesce(func.sum(GuestPosters.copies), 0),
        ).where(GuestPosters.event_id == event.id)
    )).one()
    by_moment: Counter = Counter()
    by_format: Counter = Counter()
    for (renders_json,) in (await session.execute(
        select(GuestPosters.renders).where(GuestPosters.event_id == event.id, GuestPosters.made_at.isnot(None))
    )).all():
        moments_seen = set()
        for fmt, state in (renders_json or {}).items():
            if isinstance(state, dict) and state.get("state") == "ready":
                by_format[fmt] += 1
                if state.get("moment"):
                    moments_seen.add(state["moment"])
        for m in moments_seen:
            by_moment[m] += 1

    guests = select(TrackingLinks.id).where(TrackingLinks.event_id == event.id, TrackingLinks.kind == poster_links.GUEST)
    clicks = dict((await session.execute(
        select(TrackingClicks.link_id, func.count()).where(TrackingClicks.link_id.in_(guests)).group_by(TrackingClicks.link_id)
    )).all())
    total_clicks = sum(int(n) for n in clicks.values())
    uniques = await session.scalar(
        select(func.count(func.distinct(TrackingClicks.visitor))).where(TrackingClicks.link_id.in_(guests))
    ) or 0
    regs = {lid: (int(n), int(a)) for lid, n, a in (await session.execute(
        select(EventRegistrationsLink.tracking_link_id, func.count(), func.count().filter(EventRegistrationsLink.is_attended == True))  # noqa: E712
        .where(EventRegistrationsLink.event_id == event.id, EventRegistrationsLink.is_deleted == False,  # noqa: E712
               EventRegistrationsLink.tracking_link_id.in_(guests))
        .group_by(EventRegistrationsLink.tracking_link_id)
    )).all()}
    requests = dict((await session.execute(
        select(RegistrationRequests.tracking_link_id, func.count())
        .where(RegistrationRequests.event_id == event.id, RegistrationRequests.status != WITHDRAWN,
               RegistrationRequests.tracking_link_id.in_(guests))
        .group_by(RegistrationRequests.tracking_link_id)
    )).all())

    who = {}
    ids = set(clicks) | set(regs) | set(requests)
    if ids:
        for link_id, name, role in (await session.execute(
            select(GuestPosters.tracking_link_id, EventRegistrationsLink.full_name, EventRegistrationsLink.role)
            .join(EventRegistrationsLink, and_(EventRegistrationsLink.id == GuestPosters.registration_id))
            .where(GuestPosters.event_id == event.id, GuestPosters.tracking_link_id.in_(ids))
        )).all():
            who[link_id] = (name, getattr(role, "value", role))
    top = []
    for lid in ids:
        r, _ = regs.get(lid, (0, 0))
        q = int(requests.get(lid, 0))
        name, role = who.get(lid, ("A guest", "attendee"))
        top.append({"name": name, "role": role, "clicks": int(clicks.get(lid, 0)), "registrations": r, "requests": q})
    top.sort(key=lambda t: (-(t["registrations"] + t["requests"]), -t["clicks"], t["name"] or ""))
    anchor = await session.scalar(
        select(TrackingLinks.id).where(TrackingLinks.event_id == event.id, TrackingLinks.kind == poster_links.ANCHOR, TrackingLinks.is_deleted == False)  # noqa: E712
        .order_by(TrackingLinks.created_at).limit(1)
    )
    return {
        "enabled": bool(settings_row.enabled) if settings_row is not None else False,
        "guests_eligible": int(eligible), "posters_made": int(made), "with_photo": int(with_photo), "renders": int(renders),
        "downloads": int(downloads), "shares": int(shares), "copies": int(copies),
        "link_clicks": total_clicks, "link_uniques": int(uniques),
        "registrations": sum(n for n, _ in regs.values()), "requests": sum(int(n) for n in requests.values()),
        "checked_in": sum(a for _, a in regs.values()),
        "by_moment": {m: int(by_moment.get(m, 0)) for m in ("before", "during", "after")},
        "by_format": {f: int(by_format.get(f, 0)) for f in ("square", "portrait", "story")},
        "top": top[:TOP],
        "tracking_link_id": str(anchor) if anchor else None,
    }
