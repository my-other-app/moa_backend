"""Who may act on an event: its host, and the clubs co-hosting it.

Every club-side event route had one rule, "the event's club is the caller's
club", written in `events.access.ensure_event_access` and in a handful of
module loaders. A co-host has to pass all of them, and none of them can be
allowed to forget: so the decision is made once, here, in the auth
dependency, for every request whose path names an event (`{event_id}`).

`gate` runs on every signed-in request (app.core.auth.dependencies). When
the caller is a club, the path names an event, and the club co-hosts it
(an accepted invite), the request is either refused -- a host-only action,
403 COHOST_HOST_ONLY (cohosts.policy) -- or let through: the grant is noted
for this request, and `ensure_event_access` and the module loaders read it
(`granted`). The member's own role was decided just before, by
club_access.policy, as for any request of theirs. A caller that is neither
host nor co-host is left to the route's own check, as before.

A route that finds its event some other way (a ticket id, a body field)
asks `admit`, which looks the club up and notes the grant the same way.
"""
from __future__ import annotations

from contextvars import ContextVar
from typing import Optional

from sqlalchemy import and_, or_, select

from app.api.cohosts import policy
from app.api.cohosts.models import ACCEPTED, EventCohosts

#: (user id, event id, host club id) the current request may act on as a co-host.
_grant: ContextVar[Optional[tuple[int, int, int]]] = ContextVar("cohost_grant", default=None)


def reset() -> None:
    """Forget any grant: each request decides afresh."""
    _grant.set(None)


def note(user_id: int, event_id: int, host_club_id: int) -> None:
    _grant.set((int(user_id), int(event_id), int(host_club_id)))


def granted(event_id, user_id) -> bool:
    """The current request's caller co-hosts this event (decided by `gate` or `admit`)."""
    g = _grant.get()
    if g is None or event_id is None or user_id is None:
        return False
    return g[0] == int(user_id) and g[1] == int(event_id)


def own_club_id(event, user) -> Optional[int]:
    """The club whose own things (its library, its partners) a request on
    `event` reaches for: the caller's club when it co-hosts the event, else
    the event's club (the host, or an admin acting on it)."""
    club = _club_of(user)
    if club is not None and granted(getattr(event, "id", None), user.id):
        return club.id
    return event.club_id


def _club_of(user):
    value = getattr(user.user_type, "value", user.user_type)
    if value != "club":
        return None
    return getattr(user, "club", None)


def event_id_of(request) -> Optional[int]:
    """The event a request's path names, if it names one."""
    raw = (getattr(request, "path_params", None) or {}).get("event_id")
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


# ─── Queries ──────────────────────────────────────────────────────────────


def accepted(event_id_col=None, club_id=None):
    """The condition for an accepted row (of this event column / club)."""
    conds = [EventCohosts.status == ACCEPTED]
    if event_id_col is not None:
        conds.append(EventCohosts.event_id == event_id_col)
    if club_id is not None:
        conds.append(EventCohosts.club_id == club_id)
    return and_(*conds)


def cohosted_event_ids(club_id: int):
    """The ids of the events this club co-hosts, as a subquery."""
    return select(EventCohosts.event_id).where(EventCohosts.club_id == club_id, EventCohosts.status == ACCEPTED)


def hosted_or_cohosted(club_id: int):
    """`Events` rows this club hosts or co-hosts: a WHERE condition."""
    from app.api.events.models import Events

    return or_(Events.club_id == club_id, Events.id.in_(cohosted_event_ids(club_id)))


async def is_cohost(session, event_id: int, club_id: int) -> bool:
    return bool(await session.scalar(
        select(EventCohosts.id).where(
            EventCohosts.event_id == event_id, EventCohosts.club_id == club_id, EventCohosts.status == ACCEPTED,
        ).limit(1)
    ))


async def cohost_club_ids(session, event_id: int) -> list[int]:
    """The event's co-hosts' club ids, in the order they accepted."""
    rows = await session.scalars(
        select(EventCohosts.club_id)
        .where(EventCohosts.event_id == event_id, EventCohosts.status == ACCEPTED)
        .order_by(EventCohosts.responded_at, EventCohosts.id)
    )
    return list(rows)


async def viewer_role(session, event, user) -> Optional[str]:
    """"host", "cohost", or None, for the club signed in (or one of its team)."""
    if user is None:
        return None
    club = _club_of(user)
    if club is None:
        return None
    if event.club_id == club.id:
        return "host"
    if granted(event.id, user.id) or await is_cohost(session, event.id, club.id):
        return "cohost"
    return None


def _logo(value, style: str):
    if style == "url":
        if not isinstance(value, dict):
            return None
        for k in ("thumbnail", "medium", "original"):
            if value.get(k):
                return value[k]
        return None
    return value if isinstance(value, dict) else None


async def refs(session, event_ids, *, logo: str = "sizes") -> dict[int, list[dict]]:
    """Each event's co-hosts, `{club_id, name, slug, logo}`, in the order they
    accepted, for a page of events in one statement. `logo`: "sizes" (the
    image's dict, as the /events and /clubs payloads carry a club's logo) or
    "url" (one URL, as /discover's do). Deleted or unverified clubs are left out."""
    from app.api.clubs.models import Clubs

    ids = sorted({int(i) for i in event_ids or () if i is not None})
    if not ids:
        return {}
    # The club as a row, not its columns: an image column can't be read on its own (its type is a dict's).
    rows = (await session.execute(
        select(EventCohosts.event_id, Clubs)
        .join(Clubs, Clubs.id == EventCohosts.club_id)
        .where(
            EventCohosts.event_id.in_(ids), EventCohosts.status == ACCEPTED,
            Clubs.is_deleted == False, Clubs.is_verified == True,  # noqa: E712
        )
        .order_by(EventCohosts.event_id, EventCohosts.responded_at, EventCohosts.id)
    )).all()
    out: dict[int, list[dict]] = {}
    for event_id, club in rows:
        out.setdefault(event_id, []).append(dict(club_id=club.id, name=club.name, slug=club.slug, logo=_logo(club.logo, logo)))
    return out


async def refs_by_club_ids(session, by_event: dict, *, logo: str = "sizes") -> dict[int, list[dict]]:
    """`refs`, for events whose co-hosts' club ids are already known (in
    order): one statement for the clubs."""
    from app.api.clubs.models import Clubs

    wanted = {int(c) for ids in by_event.values() for c in ids}
    if not wanted:
        return {}
    clubs = {
        c.id: c for c in await session.scalars(
            select(Clubs).where(Clubs.id.in_(wanted), Clubs.is_deleted == False, Clubs.is_verified == True)  # noqa: E712
        )
    }
    return {
        int(event_id): [
            dict(club_id=c.id, name=c.name, slug=c.slug, logo=_logo(c.logo, logo))
            for c in (clubs.get(int(i)) for i in ids) if c is not None
        ]
        for event_id, ids in by_event.items()
    }


async def attach(session, items: list, *, logo: str = "sizes") -> list:
    """Fill `cohosts` on a page of event dicts (each with an `id`), in one statement."""
    shared = await refs(session, [i.get("id") for i in items if isinstance(i, dict)], logo=logo)
    for i in items:
        if isinstance(i, dict) and i.get("id") is not None:
            i["cohosts"] = shared.get(int(i["id"]), [])
    return items


async def refs_for(session, event_id: int, *, logo: str = "sizes") -> list[dict]:
    return (await refs(session, [event_id], logo=logo)).get(int(event_id), [])


async def refuse_if_cohost(session, club_id: int, event_id, what: str) -> None:
    """For a club-level route naming an event in its body (an expense, a
    discount code): a co-host is told it is the host's to do, rather than
    that the event does not exist. Raises 403 COHOST_HOST_ONLY, or returns."""
    if event_id is None or not await is_cohost(session, int(event_id), club_id):
        return
    host = await _host_of(session, int(event_id))
    from app.response import CustomHTTPException

    raise CustomHTTPException(
        status_code=403,
        message=f"{what} for this event are {host.name if host else 'the host'}'s: they host it.",
        error_code="COHOST_HOST_ONLY",
    )


# ─── The gate ─────────────────────────────────────────────────────────────


def _refuse(host_name: str):
    from app.response import CustomHTTPException

    return CustomHTTPException(
        status_code=403,
        message=policy.HOST_ONLY_MESSAGE.format(host=host_name or "the host club"),
        error_code="COHOST_HOST_ONLY",
    )


async def _host_of(session, event_id: int):
    from app.api.clubs.models import Clubs
    from app.api.events.models import Events

    return (await session.execute(
        select(Events.club_id, Clubs.name)
        .outerjoin(Clubs, Clubs.id == Events.club_id)
        .where(Events.id == event_id, Events.is_deleted == False)  # noqa: E712
    )).first()


async def gate(session, request, user) -> None:
    """Run on every signed-in request: see the module's doc."""
    reset()
    club = _club_of(user)
    if club is None:
        return
    event_id = event_id_of(request)
    if event_id is None:
        return
    host = await _host_of(session, event_id)
    if host is None or host.club_id is None or host.club_id == club.id:
        return
    if not await is_cohost(session, event_id, club.id):
        return
    if policy.host_only(request.method, request.url.path):
        raise _refuse(host.name)
    note(user.id, event_id, host.club_id)
    try:
        request.state.cohost_of = host.club_id
    except Exception:  # noqa: BLE001 - a request without state still gets the grant
        pass


async def admit(session, user, event, *, method: str = "GET", path: str = "") -> bool:
    """For a route that finds its event without `{event_id}` in its path: note
    the grant when the caller's club co-hosts `event`. Refuses a host-only
    request as the gate would. True when granted."""
    club = _club_of(user)
    if club is None or event is None or getattr(event, "club_id", None) in (None, club.id):
        return False
    if not await is_cohost(session, event.id, club.id):
        return False
    if path and policy.host_only(method, path):
        host = await _host_of(session, event.id)
        raise _refuse(host.name if host else "")
    note(user.id, event.id, event.club_id)
    return True
