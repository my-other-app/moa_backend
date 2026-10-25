"""A person's yes to a club's updates on WhatsApp: what a promotion from the
club's own number needs (broadcasts.py).

Meta's opt-in rule: consent may be gathered off WhatsApp, but it must name
the business and say plainly that the person is opting in. So the checkbox
says exactly `text_for(club)` -- "Get updates from <club> on WhatsApp" --
and what is kept is those words, with TEXT_VERSION, the source, the person
(their account, and the registration it came from) and their number.

Where it comes from (the field contract, docs/OPERATIONS.md section 21):

- the registration form: `club_updates_whatsapp: true` in the body of
  POST /events/registration/{event}/register, with a `phone` WhatsApp can
  reach (422 WHATSAPP_NUMBER otherwise, as `whatsapp_opt_in`). False or
  left out records nothing and takes nothing back.
- following a club: `{"club_updates_whatsapp": true}` as the body of
  POST /clubs/follow/{club}; the number is the follower's profile's
  (read again when a promotion goes, if it had none). `false` takes it back;
  left out, nothing changes. Already following, the answer is the follow as
  it stands, with the consent recorded.

Withdrawn by: unfollowing the club (consent and follow go together), the
follow body's `false`, and Meta's 131050 on a promotion (the person stopped
the club's marketing in WhatsApp). STOP replies and Meta's user_preferences
webhook are the opt-out store's (optout.py), which every broadcast asks too.

Whether the box is shown at all is `offered` -- `whatsapp_updates` on the
club's public payloads (the discover club and event pages, GET /clubs/info
and GET /events/info's club): true only while a broadcast could go, so
nobody is asked for a yes the club cannot use. GET /clubs/info's
`user_data.club_updates_whatsapp` is the signed-in person's own yes, for the
follower's switch.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError

from app.api.club_whatsapp.reach_models import SOURCE_FOLLOW, SOURCE_REGISTRATION, ClubWhatsAppConsents

#: The version of the checkbox's words; a change of words is a new version.
TEXT_VERSION = "2026-10-25"


def text_for(club_name: Optional[str]) -> str:
    """The checkbox's words, naming the club (Meta's opt-in rule)."""
    return f"Get updates from {(club_name or 'this club').strip()} on WhatsApp"


def require_number(phone: Optional[str]) -> None:
    """A tick needs a number WhatsApp can reach: 422 WHATSAPP_NUMBER (keyed
    by the `phone` field, as the website reads a registration's errors)."""
    from app.core import whatsapp
    from app.response import CustomHTTPException

    number, why = whatsapp.normalise(phone)
    if number is None:
        raise CustomHTTPException(
            422, message="To get the club's updates on WhatsApp, enter a mobile number WhatsApp can reach, or untick the box.",
            error_code="WHATSAPP_NUMBER", errors={"phone": f"WhatsApp can't reach this number: {why}."},
        )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _who(user_id: Optional[int], wa_id: Optional[str], bsuid: Optional[str] = None) -> list:
    C = ClubWhatsAppConsents
    out = []
    if user_id:
        out.append(C.user_id == user_id)
    if wa_id:
        out.append(C.wa_id == wa_id)
    if bsuid:
        out.append(C.bsuid == bsuid)
    return out


async def give(session, club_id: int, *, source: str, club_name: str, user_id: Optional[int] = None, registration_id=None,
               phone: Optional[str] = None) -> Optional[ClubWhatsAppConsents]:
    """A yes kept (in the caller's transaction): the person's live consent
    for the club, made or -- given before -- filled in with what this one
    knows. None when there is nobody to keep it for."""
    from app.api.club_whatsapp import inbox

    wa_id = inbox.wa_of(phone) if phone else None
    who = _who(user_id, wa_id)
    if not who:
        return None
    C = ClubWhatsAppConsents
    row = await session.scalar(
        select(C).where(C.club_id == club_id, C.withdrawn_at.is_(None), or_(*who)).order_by(C.id).limit(1)
    )
    if row is not None:
        try:
            async with session.begin_nested():
                row.user_id = row.user_id or user_id
                row.wa_id = row.wa_id or wa_id
                row.registration_id = row.registration_id or registration_id
                await session.flush()
        except IntegrityError:
            # Someone else's live consent has that number already: theirs stands, and so does this.
            await session.refresh(row)
        return row
    row = C(club_id=club_id, user_id=user_id, registration_id=registration_id, wa_id=wa_id, source=source,
            text_version=TEXT_VERSION, consent_text=text_for(club_name)[:300], created_at=_now())
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        # The same person said yes in another request at the same moment: one live consent.
        return None
    return row


async def withdraw(session, club_id: int, *, source: str, user_id: Optional[int] = None, wa_id: Optional[str] = None,
                   bsuid: Optional[str] = None) -> int:
    """Every live consent of this person's for the club, withdrawn (in the
    caller's transaction). How many."""
    who = _who(user_id, wa_id, bsuid)
    if not who:
        return 0
    C = ClubWhatsAppConsents
    result = await session.execute(
        update(C).where(C.club_id == club_id, C.withdrawn_at.is_(None), or_(*who))
        .values(withdrawn_at=_now(), withdrawn_source=source[:32]).execution_options(synchronize_session=False)
    )
    return int(result.rowcount or 0)


async def live(session, club_id: int, *, user_id: Optional[int] = None, wa_id: Optional[str] = None, bsuid: Optional[str] = None) -> bool:
    """Whether this person's yes still stands."""
    who = _who(user_id, wa_id, bsuid)
    if not who:
        return False
    C = ClubWhatsAppConsents
    return await session.scalar(
        select(C.id).where(C.club_id == club_id, C.withdrawn_at.is_(None), or_(*who)).limit(1)
    ) is not None


async def offered(session, club_id: Optional[int]) -> bool:
    """`whatsapp_updates`: whether the club's pages offer the box -- the club
    has an ACTIVE number of its own on a plan with `whatsapp_own`, so a
    broadcast could go (broadcasts._sending_account). Nothing else about the
    connection is said. One indexed read for a club without a number; the
    plan is asked only for one with an active number. Who is looking never
    changes it: an admin, whom the plan gates let through, sees what anyone
    sees, since a broadcast goes from the club, not from them."""
    from app.api.club_whatsapp.models import ACTIVE, ClubWhatsAppAccounts
    from app.api.club_whatsapp.service import FEATURE
    from app.api.subscriptions import gates

    if club_id is None:
        return False
    active = await session.scalar(
        select(ClubWhatsAppAccounts.id).where(ClubWhatsAppAccounts.club_id == club_id, ClubWhatsAppAccounts.status == ACTIVE).limit(1)
    )
    if active is None:
        return False
    if not gates.enforcing():
        return True
    return (await gates.entitlements(session, club_id)).allows(FEATURE)


async def from_registration(session, registration_id, wanted: Optional[bool]) -> bool:
    """`club_updates_whatsapp` on a registration: true keeps the yes for the
    event's club (and commits); false or absent does nothing. True when kept."""
    from app.api.clubs.models import Clubs
    from app.api.events.models import EventRegistrationsLink as R
    from app.api.events.models import Events

    if not wanted or registration_id is None:
        return False
    row = (await session.execute(
        select(R.id, R.user_id, R.phone, Events.club_id, Clubs.name)
        .join(Events, Events.id == R.event_id).join(Clubs, Clubs.id == Events.club_id)
        .where(R.id == registration_id)
    )).first()
    if row is None or row[3] is None:
        return False
    kept = await give(session, row[3], source=SOURCE_REGISTRATION, club_name=row[4], user_id=row[1], registration_id=row[0], phone=row[2])
    await session.commit()
    return kept is not None


async def from_follow(session, club_id: int, user, wanted: Optional[bool]) -> Optional[bool]:
    """`club_updates_whatsapp` when following: true keeps the yes (the
    follower's profile's number), false takes it back, None leaves it. Commits."""
    from app.api.clubs.models import Clubs

    if wanted is None:
        return None
    if wanted:
        name = await session.scalar(select(Clubs.name).where(Clubs.id == club_id))
        kept = await give(session, club_id, source=SOURCE_FOLLOW, club_name=name, user_id=user.id, phone=getattr(user, "phone", None))
        await session.commit()
        return kept is not None
    await withdraw(session, club_id, source=SOURCE_FOLLOW, user_id=user.id)
    await session.commit()
    return False


async def on_unfollow(session, club_id: int, user_id: int) -> int:
    """Unfollowing takes the person's yes to the club's updates back with it (the caller commits)."""
    return await withdraw(session, club_id, source="unfollow", user_id=user_id)
