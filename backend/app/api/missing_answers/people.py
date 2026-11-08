"""The people an event's answers belong to, how a URL names one, and the
personal link that lets one answer without signing in.

A person (contract §0) is a live registration -- not cancelled (deleted),
its ticket not revoked -- or an open registration request: pending, or
confirmed and not yet a live registration (a paid ticket's hold). A
request that became a registration is that registration, counted once.

`ref` is what the clients pass around: the registration's id, or "q" and the
request's id. They never parse it.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional

from sqlalchemy import and_, false, select

from app.api.approvals.models import OPEN as REQUEST_OPEN
from app.api.approvals.models import RegistrationRequests
from app.api.events.models import EventRegistrationsLink
from app.api.missing_answers.models import REGISTRATION, REQUEST
from app.config import settings
from app.core.mail import links

R = EventRegistrationsLink
Q = RegistrationRequests


@dataclass
class Person:
    kind: str
    id: uuid.UUID
    answers: dict
    full_name: str = ""
    email: Optional[str] = None
    phone: Optional[str] = None
    user_id: Optional[int] = None
    whatsapp_opt_in: bool = False
    wa_bounced_at: Optional[datetime] = None
    email_bounced_at: Optional[datetime] = None
    #: A ticket that gets them in (free, or paid): what WhatsApp may be sent about.
    live_ticket: bool = True
    ticket_id: Optional[str] = None
    #: A request's status link's secret.
    token: Optional[str] = None

    @property
    def ref(self) -> str:
        return ref_of(self.kind, self.id)

    @property
    def key(self) -> tuple[str, uuid.UUID]:
        return (self.kind, self.id)

    @property
    def first_name(self) -> str:
        return (self.full_name or "").strip().split(" ")[0] if self.full_name else ""


def ref_of(kind: str, id_) -> str:
    return str(id_) if kind == REGISTRATION else f"q{id_}"


def parse_ref(ref) -> Optional[tuple[str, uuid.UUID]]:
    """(kind, id) for a ref, or None when it names nobody."""
    text = str(ref or "").strip()
    kind = REGISTRATION
    if text[:1] in ("q", "Q"):
        kind, text = REQUEST, text[1:]
    try:
        return kind, uuid.UUID(text)
    except ValueError:
        return None


# ─── The personal link (contract §2.2) ───────────────────────────────────


def _message(event_id, ref: str) -> str:
    return f"answers:{int(event_id)}:{ref}"


def signature(event_id, ref: str) -> str:
    return links.sign(_message(event_id, ref))


def signature_ok(event_id, ref: str, k: Optional[str]) -> bool:
    return links.verify(_message(event_id, ref), k or "")


def link_suffix(event_id, ref: str) -> str:
    """What follows /answers/ in the link: `{event_id}/{ref}?k={sig}` -- the
    WhatsApp template's button fills exactly this."""
    return f"{int(event_id)}/{ref}?k={signature(event_id, ref)}"


def personal_link(event_id, ref: str) -> str:
    return f"{settings.PUBLIC_SITE_URL.rstrip('/')}/answers/{link_suffix(event_id, ref)}"


# ─── Reading them ────────────────────────────────────────────────────────


def _reg_person(row, has_fee: bool) -> Person:
    (rid, answers, name, email, phone, user_id, opted_in, wa_bounced, email_bounced, is_paid, actual, ticket_id) = row
    owes = actual is not None and float(actual) > 0
    return Person(
        kind=REGISTRATION, id=rid, answers=answers if isinstance(answers, dict) else {}, full_name=name or "",
        email=email, phone=phone, user_id=user_id, whatsapp_opt_in=bool(opted_in), wa_bounced_at=wa_bounced,
        email_bounced_at=email_bounced, live_ticket=(not has_fee) or bool(is_paid) or not owes, ticket_id=ticket_id,
    )


def _req_person(row) -> Person:
    qid, answers, name, email, phone, user_id, opted_in, token = row
    return Person(
        kind=REQUEST, id=qid, answers=answers if isinstance(answers, dict) else {}, full_name=name or "",
        email=email, phone=phone, user_id=user_id, whatsapp_opt_in=bool(opted_in), token=token,
    )


#: The most ids `load` names in one IN list.
MAX_IN = 2000

_REG_COLUMNS = (
    R.id, R.additional_details, R.full_name, R.email, R.phone, R.user_id, R.whatsapp_opt_in, R.wa_bounced_at,
    R.email_bounced_at, R.is_paid, R.actual_amount, R.ticket_id,
)
_REQ_COLUMNS = (Q.id, Q.answers, Q.full_name, Q.email, Q.phone, Q.user_id, Q.whatsapp_opt_in, Q.token)


def _live_registrations(event_id: int):
    return select(*_REG_COLUMNS).where(
        R.event_id == event_id, R.is_deleted == false(), R.ticket_revoked_at.is_(None),
    )


def _open_requests(event_id: int):
    # The registrations' table as a table: the soft-delete filter has nothing
    # to put in a WHERE that would drop the requests without one.
    reg = R.__table__
    return (
        select(*_REQ_COLUMNS)
        .select_from(Q)
        .outerjoin(reg, and_(reg.c.id == Q.registration_id, reg.c.is_deleted == false()))
        .where(Q.event_id == event_id, Q.status.in_(REQUEST_OPEN), reg.c.id.is_(None))
    )


def split_refs(refs: Optional[Iterable]) -> tuple[list[uuid.UUID], list[uuid.UUID]]:
    regs, reqs = [], []
    for r in refs or []:
        parsed = parse_ref(r) if not isinstance(r, tuple) else r
        if parsed is None:
            continue
        (regs if parsed[0] == REGISTRATION else reqs).append(parsed[1])
    return regs, reqs


async def load(
    session, event, *, registrations: bool = True, requests: bool = True, refs: Optional[Iterable] = None,
) -> list[Person]:
    """Everyone who counts for the event (or, with `refs`, those of them),
    registrations first, each in the order they came."""
    out: list[Person] = []
    reg_ids = req_ids = None
    if refs is not None:
        refs = list(refs)
        if len(refs) > MAX_IN:
            # Too many for one IN list: everyone, then just these.
            wanted = {(r if isinstance(r, tuple) else parse_ref(r)) for r in refs}
            return [p for p in await load(session, event, registrations=registrations, requests=requests) if p.key in wanted]
        reg_ids, req_ids = split_refs(refs)
    if registrations and (reg_ids is None or reg_ids):
        stmt = _live_registrations(event.id)
        if reg_ids is not None:
            stmt = stmt.where(R.id.in_(reg_ids))
        rows = (await session.execute(stmt.order_by(R.created_at, R.id))).all()
        out += [_reg_person(r, bool(event.has_fee)) for r in rows]
    if requests and (req_ids is None or req_ids):
        stmt = _open_requests(event.id)
        if req_ids is not None:
            stmt = stmt.where(Q.id.in_(req_ids))
        rows = (await session.execute(stmt.order_by(Q.submitted_at, Q.id))).all()
        out += [_req_person(r) for r in rows]
    return out


async def load_one(session, event, kind: str, id_) -> Optional[Person]:
    """The person, while they still count; None otherwise."""
    found = await load(session, event, registrations=kind == REGISTRATION, requests=kind == REQUEST, refs=[(kind, id_)])
    return found[0] if found else None


async def exists(session, event_id: int, kind: str, id_) -> bool:
    """Whether the ref names a row of this event at all, counted or not
    (a cancelled registration, a declined request): a link to one is not a
    bad link, it just has nothing to ask."""
    if kind == REGISTRATION:
        stmt = select(R.id).where(R.id == id_, R.event_id == event_id).execution_options(include_deleted=True)
    else:
        stmt = select(Q.id).where(Q.id == id_, Q.event_id == event_id)
    return (await session.scalar(stmt.limit(1))) is not None


def counts(people: list[Person]) -> dict:
    return {
        "registrations": sum(1 for p in people if p.kind == REGISTRATION),
        "requests": sum(1 for p in people if p.kind == REQUEST),
    }
