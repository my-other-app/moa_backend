"""The club's WhatsApp inbox as a team's: who answers a conversation, the
notes they leave each other, and the answers they keep.

- Assigning: a conversation goes to the club's owner or to a member with
  the WhatsApp permission (no one else can read it). The inbox lists Mine,
  Unassigned, and each person's. The new assignee is mailed -- who assigned
  it and the contact's name, nothing a message says -- unless they turned
  that off, or assigned it to themselves. Audited, without content.
- Notes: up to 2,000 characters on a conversation, with who wrote them and
  when; shown only to the inbox's people, never sent, deleted with the
  conversation (retention). Their author or the owner may delete one.
  Audited without their words.
- Saved replies: the club's own answers (a title, up to 4,096 characters, an
  optional /shortcut the composer's "/" finds). "{name}" in one becomes the
  contact's display name when the composer puts it in.

Every route here is the inbox's (access.require_inbox): the owner and the
members with the permission; never an admin acting as the club.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select

from app.api.club_access import service as club_access
from app.api.club_whatsapp import access
from app.api.club_whatsapp.inbox_models import (
    NOTE_MAX,
    REPLY_BODY_MAX,
    REPLY_TITLE_MAX,
    ClubWhatsAppNotes,
    ClubWhatsAppSavedReplies,
)
from app.api.club_whatsapp.models import ClubWhatsAppConversations
from app.response import CustomHTTPException

FILTERS = ("all", "open", "unanswered", "unread", "closed", "mine", "unassigned", "blocked")
SHORTCUT = re.compile(r"^[a-z0-9][a-z0-9_-]{0,30}$")
#: Saved replies a club keeps, at most.
REPLIES_MAX = 200


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _refuse(code: int, message: str, error_code: str, errors: Optional[dict] = None) -> CustomHTTPException:
    return CustomHTTPException(status_code=code, message=message, error_code=error_code, errors=errors)


@dataclass
class Viewer:
    """Who is asking: the club's own account (`member_id` None), or a member."""

    member_id: Optional[int]
    user_id: Optional[int]
    name: str

    @property
    def owner(self) -> bool:
        return self.member_id is None

    @property
    def key(self) -> str:
        return "owner" if self.member_id is None else f"member:{self.member_id}"


def viewer(request, user) -> Viewer:
    member = club_access.current_member(request)
    return Viewer(
        member_id=getattr(member, "id", None),
        user_id=None if member is not None else getattr(user, "id", None),
        name=access.actor_name(request, user),
    )


# ─── The team ────────────────────────────────────────────────────────────


async def team(session, club_id: int, me: Viewer) -> list[dict]:
    """Who a conversation can be assigned to: the owner and the members with
    the WhatsApp permission."""
    from app.api.club_whatsapp import alerts_inbox

    return [
        {"key": "owner" if p.member_id is None else f"member:{p.member_id}", "kind": "owner" if p.member_id is None else "member",
         "member_id": p.member_id, "name": p.name if p.member_id is not None else "The club's owner",
         "me": p.member_id == me.member_id}
        for p in await alerts_inbox.inbox_people(session, club_id)
    ]


def parse_assignee(raw: Optional[str]) -> tuple[Optional[bool], Optional[int]]:
    """"owner" -> (True, None); "member:5" or "5" -> (False, 5); empty -> (None, None)."""
    value = (raw or "").strip().lower()
    if not value:
        return None, None
    if value == "owner":
        return True, None
    digits = value.removeprefix("member:")
    if digits.isdigit():
        return False, int(digits)
    raise _refuse(422, 'assignee is "owner" or a member\'s id', "INVALID_ASSIGNEE")


def filter_clauses(filter: str, assignee: Optional[str], me: Viewer) -> tuple[str, list]:
    """The inbox's base filter (inbox.FILTERS) and the extra conditions for
    ours: Mine, Unassigned, Blocked, and a person's. Blocked conversations
    are in the Blocked filter alone."""
    C = ClubWhatsAppConversations
    if filter not in FILTERS:
        raise _refuse(422, f"filter is one of {', '.join(FILTERS)}", "INVALID_FILTER")
    extra: list = []
    base = filter
    if filter == "blocked":
        base = "all"
        extra.append(C.blocked_at.isnot(None))
    else:
        extra.append(C.blocked_at.is_(None))
    if filter == "mine":
        base = "all"
        extra.append(C.assigned_owner == True if me.owner else C.assigned_member_id == me.member_id)  # noqa: E712
    elif filter == "unassigned":
        base = "all"
        extra += [C.assigned_owner == False, C.assigned_member_id.is_(None)]  # noqa: E712
    owner, member_id = parse_assignee(assignee)
    if owner:
        extra.append(C.assigned_owner == True)  # noqa: E712
    elif member_id is not None:
        extra.append(C.assigned_member_id == member_id)
    return base, extra


async def annotate(session, club_id: int, convs: list) -> None:
    """What the inbox shows of each conversation beyond what it holds: its
    opt-out, its assignee's name, how many notes. Kept on the rows for
    inbox.conversation_out."""
    from app.api.club_access.models import ClubMembers
    from app.api.club_whatsapp import optout

    if not convs:
        return
    opt = await optout.statuses(session, club_id, convs)
    member_ids = {c.assigned_member_id for c in convs if c.assigned_member_id}
    names = {}
    if member_ids:
        names = {m[0]: (m[1] or m[2] or "A team member") for m in (await session.execute(
            select(ClubMembers.id, ClubMembers.full_name, ClubMembers.email).where(ClubMembers.id.in_(member_ids))
        )).all()}
    N = ClubWhatsAppNotes
    notes = dict((await session.execute(
        select(N.conversation_id, func.count()).where(N.conversation_id.in_([c.id for c in convs])).group_by(N.conversation_id)
    )).all())
    for c in convs:
        c._wa_team = {"opt_out": opt.get(c.id), "assignee_name": names.get(c.assigned_member_id), "notes": int(notes.get(c.id, 0))}


def team_out(conv) -> dict:
    """The fields inbox.conversation_out adds: assignee, block, opt-out, notes."""
    extra = getattr(conv, "_wa_team", None) or {}
    assignee = None
    if conv.assigned_owner:
        assignee = {"kind": "owner", "member_id": None, "name": "The club's owner", "key": "owner"}
    elif conv.assigned_member_id:
        assignee = {"kind": "member", "member_id": conv.assigned_member_id, "key": f"member:{conv.assigned_member_id}",
                    "name": extra.get("assignee_name") or "A team member"}
    return {
        "assignee": assignee,
        "assigned_at": conv.assigned_at,
        "blocked": {"at": conv.blocked_at, "meta": bool(conv.blocked_meta), "by": conv.blocked_by_name} if conv.blocked_at else None,
        "opt_out": extra.get("opt_out"),
        "notes": extra.get("notes", 0),
    }


# ─── Assigning ───────────────────────────────────────────────────────────


async def assign(session, club_id: int, conv: ClubWhatsAppConversations, *, owner: bool, member_id: Optional[int],
                 me: Viewer, user, request) -> bool:
    """Assign the conversation (owner, a member, or nobody). The member must
    have the WhatsApp permission. True when it changed."""
    from app.api.audit import service as audit
    from app.api.club_access.models import ClubMembers
    from app.api.club_whatsapp import alerts_inbox

    if owner and member_id is not None:
        raise _refuse(422, "Assign it to the owner or to a member, not both.", "INVALID_ASSIGNEE")
    if member_id is not None:
        m = await session.get(ClubMembers, member_id)
        if m is None or m.club_id != club_id or m.status != "active" or not m.whatsapp:
            raise _refuse(422, "Only the owner and members with the WhatsApp permission can be given a conversation.",
                          "INVALID_ASSIGNEE", {"member_id": member_id})
    before = "owner" if conv.assigned_owner else (f"member:{conv.assigned_member_id}" if conv.assigned_member_id else None)
    after = "owner" if owner else (f"member:{member_id}" if member_id is not None else None)
    if before == after:
        return False
    conv.assigned_owner = bool(owner)
    conv.assigned_member_id = member_id
    conv.assigned_at = _now() if after else None
    await audit.record(
        session, actor=user, action="club_whatsapp.assign", entity_type="club", entity_id=club_id,
        summary="Assigned a WhatsApp conversation" if after else "Unassigned a WhatsApp conversation",
        details={"conversation_id": conv.id, "from": before, "to": after, "member_id": me.member_id}, request=request,
    )
    if after and after != me.key:
        contact = alerts_inbox.display_name(conv.contact_name, conv.profile_name, conv.username, conv.wa_id)
        await alerts_inbox.tell_assignee(session, club_id, member_id, contact=contact, by=me.name)
    return True


# ─── Notes ───────────────────────────────────────────────────────────────


def note_out(n: ClubWhatsAppNotes, me: Viewer) -> dict:
    mine = (me.member_id is not None and n.author_member_id == me.member_id) or (me.owner and n.author_member_id is None)
    return {"id": n.id, "conversation_id": n.conversation_id, "body": n.body, "author": n.author_name or "Someone",
            "created_at": n.created_at, "mine": mine, "can_delete": mine or me.owner}


async def notes(session, conv: ClubWhatsAppConversations) -> list[ClubWhatsAppNotes]:
    N = ClubWhatsAppNotes
    return list((await session.scalars(select(N).where(N.conversation_id == conv.id).order_by(N.id))).all())


async def add_note(session, club_id: int, conv: ClubWhatsAppConversations, body: str, me: Viewer, user, request) -> ClubWhatsAppNotes:
    from app.api.audit import service as audit

    text = (body or "").strip()
    if not text:
        raise _refuse(422, "Write the note.", "EMPTY_NOTE")
    if len(text) > NOTE_MAX:
        raise _refuse(422, f"A note is at most {NOTE_MAX} characters.", "NOTE_TOO_LONG")
    note = ClubWhatsAppNotes(conversation_id=conv.id, club_id=club_id, body=text, author_user_id=me.user_id,
                             author_member_id=me.member_id, author_name=me.name[:120], created_at=_now())
    session.add(note)
    await session.flush()
    await audit.record(
        session, actor=user, action="club_whatsapp.note", entity_type="club", entity_id=club_id,
        summary="Added a note to a WhatsApp conversation",
        details={"conversation_id": conv.id, "note_id": note.id, "member_id": me.member_id}, request=request,
    )
    return note


async def delete_note(session, club_id: int, conv: ClubWhatsAppConversations, note_id: int, me: Viewer, user, request) -> None:
    from app.api.audit import service as audit

    note = await session.get(ClubWhatsAppNotes, note_id)
    if note is None or note.club_id != club_id or note.conversation_id != conv.id:
        raise _refuse(404, "No such note.", "NOTE_NOT_FOUND")
    if not note_out(note, me)["can_delete"]:
        raise _refuse(403, "Only whoever wrote a note, or the club's owner, can delete it.", "NOTE_NOT_YOURS")
    await session.delete(note)
    await audit.record(
        session, actor=user, action="club_whatsapp.note_delete", entity_type="club", entity_id=club_id,
        summary="Deleted a note on a WhatsApp conversation",
        details={"conversation_id": conv.id, "note_id": note_id, "member_id": me.member_id}, request=request,
    )


# ─── Saved replies ───────────────────────────────────────────────────────


def reply_out(r: ClubWhatsAppSavedReplies) -> dict:
    return {"id": r.id, "title": r.title, "body": r.body, "shortcut": r.shortcut, "updated_at": r.updated_at}


async def saved_replies(session, club_id: int) -> list[ClubWhatsAppSavedReplies]:
    R = ClubWhatsAppSavedReplies
    return list((await session.scalars(select(R).where(R.club_id == club_id).order_by(func.lower(R.title), R.id))).all())


def _clean_reply(title: Optional[str], body: Optional[str], shortcut: Optional[str]) -> tuple[str, str, Optional[str]]:
    t = " ".join((title or "").split())
    b = (body or "").strip()
    s = (shortcut or "").strip().lstrip("/").lower() or None
    if not t:
        raise _refuse(422, "Give it a title.", "INVALID_SAVED_REPLY", {"field": "title"})
    if len(t) > REPLY_TITLE_MAX:
        raise _refuse(422, f"A title is at most {REPLY_TITLE_MAX} characters.", "INVALID_SAVED_REPLY", {"field": "title"})
    if not b:
        raise _refuse(422, "Write the reply.", "INVALID_SAVED_REPLY", {"field": "body"})
    if len(b) > REPLY_BODY_MAX:
        raise _refuse(422, f"A reply is at most {REPLY_BODY_MAX} characters.", "INVALID_SAVED_REPLY", {"field": "body"})
    if s is not None and not SHORTCUT.match(s):
        raise _refuse(422, "A shortcut is one word: letters, digits, - and _ (like /parking).", "INVALID_SAVED_REPLY",
                      {"field": "shortcut"})
    return t, b, s


async def _shortcut_free(session, club_id: int, shortcut: Optional[str], but: Optional[int] = None) -> None:
    if shortcut is None:
        return
    R = ClubWhatsAppSavedReplies
    taken = await session.scalar(select(R.id).where(R.club_id == club_id, R.shortcut == shortcut, R.id != (but or 0)))
    if taken is not None:
        raise _refuse(409, f"/{shortcut} is already another saved reply's shortcut.", "SHORTCUT_TAKEN", {"field": "shortcut"})


async def save_reply(session, club_id: int, *, title, body, shortcut, me: Viewer, reply_id: Optional[int] = None) -> ClubWhatsAppSavedReplies:
    R = ClubWhatsAppSavedReplies
    t, b, s = _clean_reply(title, body, shortcut)
    await _shortcut_free(session, club_id, s, reply_id)
    if reply_id is None:
        if await session.scalar(select(func.count()).select_from(R).where(R.club_id == club_id)) >= REPLIES_MAX:
            raise _refuse(409, f"A club keeps at most {REPLIES_MAX} saved replies.", "TOO_MANY_SAVED_REPLIES")
        row = R(club_id=club_id, title=t, body=b, shortcut=s, created_by_name=me.name[:120], created_at=_now(), updated_at=_now())
        session.add(row)
    else:
        row = await session.get(R, reply_id)
        if row is None or row.club_id != club_id:
            raise _refuse(404, "No such saved reply.", "SAVED_REPLY_NOT_FOUND")
        row.title, row.body, row.shortcut, row.updated_at = t, b, s, _now()
    await session.flush()
    return row


async def delete_reply(session, club_id: int, reply_id: int) -> None:
    row = await session.get(ClubWhatsAppSavedReplies, reply_id)
    if row is None or row.club_id != club_id:
        raise _refuse(404, "No such saved reply.", "SAVED_REPLY_NOT_FOUND")
    await session.delete(row)
