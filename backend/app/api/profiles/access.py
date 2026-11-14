"""Who a person is to the one looking at them, and what that lets them see.

A profile is found only when its person is an app user whose account is
live (not deleted, not suspended), whose profile an admin has not hidden,
and when neither of the two has blocked the other: every other case is the
same 404 NOT_FOUND, so a stranger cannot tell a blocked person from one who
is not there.

Levels (the contract's "Visibility levels"), for a section of the profile:
`everyone`; `met` = either has saved the other; `mutual` = both have; `me`
= the owner only. The address and the number have their own: `nobody`,
`met`, `mutual`. The owner sees everything of their own.

A private profile shows a stranger -- anyone the owner has not saved --
only the handle, name, picture and headline (`minimal`). An older account
whose username is a reserved word has no public page (404) until it picks
a handle.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import aliased, selectinload

from app.api.profiles.models import LEVELS, NetworkPeople
from app.api.reports.models import UserBlocks
from app.api.users.models import DEFAULT_VISIBILITY, UserProfiles, Users, UserTypes
from app.response import CustomHTTPException

#: A profile's switches when the person has no profile row yet (the columns' defaults).
DEFAULTS = {
    "share_email": "nobody", "share_phone": "nobody", "is_private": False, "indexable": False,
    "notify_saves": True, "recap_emails": True, "scanning_enabled": True,
    "show_events": False, "show_certificates": True, "show_talks": True, "show_clubs": False,
}
SETTING_KEYS = ("visibility", *DEFAULTS)


def not_found() -> CustomHTTPException:
    return CustomHTTPException(404, message="We couldn't find that profile.", error_code="NOT_FOUND")


@dataclass
class Relation:
    """The viewer (None: signed out) and the owner, as the network sees them."""

    viewer_id: Optional[int] = None
    is_me: bool = False
    #: The viewer saved the owner (the viewer's network row: `network_id`).
    saved_by_me: bool = False
    #: The owner saved the viewer.
    saved_me: bool = False
    network_id: Optional[int] = None

    @property
    def met(self) -> bool:
        return self.saved_by_me or self.saved_me

    @property
    def mutual(self) -> bool:
        return self.saved_by_me and self.saved_me

    def sees(self, level: Optional[str]) -> bool:
        """Whether the viewer may see a section at `level`."""
        if self.is_me:
            return True
        level = level if level in LEVELS else "me"
        if level == "everyone":
            return True
        if level == "met":
            return self.met
        if level == "mutual":
            return self.mutual
        return False

    def shares(self, level: Optional[str]) -> bool:
        """Whether the viewer may see the address or the number at `level`."""
        if self.is_me:
            return True
        if level == "met":
            return self.met
        if level == "mutual":
            return self.mutual
        return False

    def out(self) -> Optional[dict]:
        """PublicProfile.relation: null when signed out or looking at oneself."""
        if self.viewer_id is None or self.is_me:
            return None
        return {"saved_by_me": self.saved_by_me, "saved_me": self.saved_me, "mutual": self.mutual, "network_id": self.network_id}


def settings_of(profile: Optional[UserProfiles]) -> dict:
    """The person's switches and section levels, defaults filled in."""
    out = dict(DEFAULTS)
    visibility = dict(DEFAULT_VISIBILITY)
    if profile is not None:
        for key in DEFAULTS:
            value = getattr(profile, key, None)
            if value is not None:
                out[key] = value
        stored = profile.visibility if isinstance(profile.visibility, dict) else {}
        visibility.update({k: v for k, v in stored.items() if k in visibility and v in LEVELS})
    out["visibility"] = visibility
    return out


def minimal(settings: dict, rel: Relation) -> bool:
    """A private profile, seen by someone the owner has not saved."""
    return bool(settings["is_private"]) and not rel.is_me and not rel.saved_me


def person_options():
    """What a profile reads with its person: the profile, its avatar and its organisation."""
    return (selectinload(Users.profile).options(selectinload(UserProfiles.avatar), selectinload(UserProfiles.org)),)


def live_person_where():
    """An app user's account that is live: not deleted, not suspended."""
    return (Users.user_type == UserTypes.app_user, Users.is_deleted == False, Users.is_active == True)  # noqa: E712


async def load_person(session, *, user_id: Optional[int] = None, handle: Optional[str] = None) -> Optional[Users]:
    """The live person by id or by handle (case-blind), with their profile; None when there is none."""
    q = select(Users).where(*live_person_where()).options(*person_options())
    if handle is not None:
        q = q.where(func.lower(Users.username) == handle.lower()).order_by(Users.id)
    else:
        q = q.where(Users.id == user_id)
    return await session.scalar(q.limit(1))


async def load_person_any(session, user_id: int) -> Users:
    """The signed-in person's own account, with their profile, whatever its state."""
    return await session.scalar(select(Users).where(Users.id == user_id).options(*person_options()))


def reserved_name(user: Users) -> bool:
    """An older account whose username is one of the site's reserved words
    ("support", "admin"): no public page until they pick a handle (MyProfile
    says handle_needs_change)."""
    from app.api.profiles import rules

    return (user.username or "").lower() in rules.RESERVED


def is_hidden(user: Users) -> bool:
    profile = user.profile
    return profile is not None and getattr(profile, "suspended_at", None) is not None


async def blocked_between(session, a: Optional[int], b: Optional[int]) -> bool:
    """Either of the two has blocked the other."""
    if a is None or b is None or a == b:
        return False
    return bool(await session.scalar(select(exists().where(or_(
        and_(UserBlocks.blocker_id == a, UserBlocks.blocked_id == b),
        and_(UserBlocks.blocker_id == b, UserBlocks.blocked_id == a),
    )))))


def viewer_id_of(user) -> Optional[int]:
    """Whose relation counts: a signed-in person (an app user or an admin's
    own account). A guest or a club reads a profile as anyone does."""
    if user is None:
        return None
    kind = getattr(user.user_type, "value", user.user_type)
    return user.id if kind in (UserTypes.app_user.value, UserTypes.admin.value) else None


async def visible_person(session, viewer_id: Optional[int], *, user_id: Optional[int] = None, handle: Optional[str] = None) -> Users:
    """The person, if this viewer may find them at all; else 404 NOT_FOUND
    (unknown, deleted, suspended, hidden, or blocked either way)."""
    user = await load_person(session, user_id=user_id, handle=handle)
    if user is None or is_hidden(user) or reserved_name(user):
        raise not_found()
    if viewer_id is not None and await blocked_between(session, viewer_id, user.id):
        raise not_found()
    return user


async def relation(session, viewer_id: Optional[int], owner_id: int) -> Relation:
    if viewer_id is None:
        return Relation()
    if viewer_id == owner_id:
        return Relation(viewer_id=viewer_id, is_me=True)
    rows = (await session.execute(
        select(NetworkPeople.id, NetworkPeople.owner_id).where(or_(
            and_(NetworkPeople.owner_id == viewer_id, NetworkPeople.person_id == owner_id),
            and_(NetworkPeople.owner_id == owner_id, NetworkPeople.person_id == viewer_id),
        ))
    )).all()
    rel = Relation(viewer_id=viewer_id)
    for row_id, row_owner in rows:
        if row_owner == viewer_id:
            rel.saved_by_me, rel.network_id = True, row_id
        else:
            rel.saved_me = True
    return rel


async def relations(session, viewer_id: int, owner_ids) -> dict[int, Relation]:
    """`relation` for many owners at once (a list of people)."""
    ids = {int(i) for i in owner_ids if i is not None and int(i) != viewer_id}
    out = {i: Relation(viewer_id=viewer_id) for i in ids}
    if not ids:
        return out
    rows = (await session.execute(
        select(NetworkPeople.id, NetworkPeople.owner_id, NetworkPeople.person_id).where(or_(
            and_(NetworkPeople.owner_id == viewer_id, NetworkPeople.person_id.in_(ids)),
            and_(NetworkPeople.person_id == viewer_id, NetworkPeople.owner_id.in_(ids)),
        ))
    )).all()
    for row_id, row_owner, row_person in rows:
        if row_owner == viewer_id:
            rel = out[row_person]
            rel.saved_by_me, rel.network_id = True, row_id
        else:
            out[row_owner].saved_me = True
    return out


def shown_person_where(column, viewer_id: int):
    """The people a list may show this viewer by `column` (a user id): live
    app users, profile not hidden, no block either way.

    Each is a correlated EXISTS, a probe per row by an index: the user by
    its primary key, a block by user_blocks' primary key or
    ix_user_blocks_blocked, a hidden profile by ix_user_profiles_hidden
    (partial: only hidden profiles are in it). As NOT IN subqueries they
    read every live profile on every call."""
    # Aliases: a query that joins users or user_profiles itself must not
    # have these probes correlate to its own rows.
    u, p, b = aliased(Users), aliased(UserProfiles), aliased(UserBlocks)
    return (
        exists().where(u.id == column, u.user_type == UserTypes.app_user, u.is_deleted == False, u.is_active == True),  # noqa: E712
        ~exists().where(b.blocker_id == viewer_id, b.blocked_id == column),
        ~exists().where(b.blocker_id == column, b.blocked_id == viewer_id),
        ~exists().where(p.user_id == column, p.suspended_at.isnot(None), p.is_deleted == False),  # noqa: E712
    )
