"""Leads: made by hand, from an import, from "Talk to us", from a club that
signed up by itself, from a support case or from mail copied to sales@;
worked by their owner through the stages; merged when added twice.

The duplicate check, everywhere a lead is made: the same name (and city,
when both have one), or the same mail domain -- the organisation's own, from
its website or its people, never a free mail service's. Making one by hand
answers 409 DUPLICATE_LEAD with the matches unless `force`.

Who may change a lead: an admin owner (reassigning too), or the sales team
member who owns it. A sales member may take an unowned one. Everyone else
reads. Every change goes to the audit log.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import and_, exists, func, or_, select, update

from app.api.leads.models import (
    ACTIVITY_KINDS,
    FOLLOW_UP_KINDS,
    LOST,
    OPEN_STAGES,
    SOURCES,
    STAGES,
    TYPES,
    WON,
    FollowUps,
    LeadActivities,
    LeadContacts,
    LeadFiles,
    LeadLostReasons,
    Leads,
)
from app.api.support import hours, teams
from app.api.support.models import SALES
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

ENTITY = "lead"
#: Mail services anyone can have an address at: never an organisation's domain.
FREEMAIL = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.in", "yahoo.in", "ymail.com", "outlook.com", "hotmail.com",
    "live.com", "msn.com", "icloud.com", "me.com", "mac.com", "aol.com", "protonmail.com", "proton.me", "rediffmail.com",
    "zoho.com", "zohomail.in", "gmx.com", "mail.com", "yandex.com", "hey.com", "fastmail.com", "tutanota.com",
})
_NOISE = {"the", "pvt", "private", "ltd", "limited", "llp", "inc", "co", "company", "corp", "corporation", "and", "of"}
USER_ACTIVITY_KINDS = ("call", "meeting", "email", "whatsapp", "note")
MAX_CONTACTS = 50


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _refuse(code: int, message: str, error_code: str, errors: Optional[dict] = None) -> CustomHTTPException:
    return CustomHTTPException(code, message=message, error_code=error_code, errors=errors)


async def audit(session, actor, action: str, lead: Leads, summary: str, details: Optional[dict] = None, request=None) -> None:
    from app.api.audit import service as audit_service

    await audit_service.record(session, actor=actor, action=f"leads.{action}", entity_type=ENTITY, entity_id=lead.id,
                               summary=summary[:255], details=details, request=request)


# ─── Keys for the duplicate check ────────────────────────────────────────


def key_of(value: Optional[str]) -> str:
    words = re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).split()
    kept = [w for w in words if w not in _NOISE] or words
    return " ".join(kept)[:200]


def domain_of(value: Optional[str]) -> Optional[str]:
    """The organisation's mail domain from an address or a website, or None
    for a free mail service's or nothing usable."""
    v = (value or "").strip().lower()
    if not v:
        return None
    if "@" in v:
        host = v.rsplit("@", 1)[1]
    else:
        host = re.sub(r"^[a-z]+://", "", v).split("/", 1)[0].split(":", 1)[0]
    host = host.removeprefix("www.").strip(".")
    if "." not in host or host in FREEMAIL or len(host) > 200:
        return None
    return host


def lead_domain(website: Optional[str], emails: Iterable[Optional[str]]) -> Optional[str]:
    for e in emails:
        d = domain_of(e)
        if d:
            return d
    return domain_of(website)


async def duplicates(session, name: str, city: Optional[str], domains: Iterable[Optional[str]], *,
                     exclude_id: Optional[int] = None, limit: int = 5) -> list[Leads]:
    L = Leads
    name_key, city_key = key_of(name), key_of(city) or None
    clauses = []
    if name_key:
        same_name = L.name_key == name_key
        # A city on both sides must agree; a lead without one matches on the name alone.
        clauses.append(and_(same_name, or_(L.city_key.is_(None), L.city_key == city_key)) if city_key else same_name)
    found_domains = [d for d in dict.fromkeys(domains) if d]
    if found_domains:
        clauses.append(L.email_domain.in_(found_domains))
    if not clauses:
        return []
    stmt = select(L).where(or_(*clauses), L.merged_into_id.is_(None)).order_by(L.updated_at.desc()).limit(limit)
    if exclude_id is not None:
        stmt = stmt.where(L.id != exclude_id)
    return list((await session.scalars(stmt)).all())


def match_out(leads: list[Leads]) -> list[dict]:
    return [{"id": d.id, "name": d.name, "city": d.city, "stage": d.stage, "type": d.type} for d in leads]


# ─── Rights ──────────────────────────────────────────────────────────────


def can_edit(rights: teams.Rights, lead: Leads) -> bool:
    return rights.owner or (rights.sales and lead.owner_id == rights.user_id)


def require_edit(rights: teams.Rights, lead: Leads, what: str = "change this lead") -> None:
    if can_edit(rights, lead):
        return
    if not rights.sales:
        rights.require(what, sales=True)
    if lead.owner_id is None:
        raise _refuse(403, "Take the lead first (set yourself as its owner).", "NOT_YOUR_LEAD")
    raise _refuse(403, f"Only its owner (or an admin owner) can {what}.", "NOT_YOUR_LEAD")


async def check_owner(session, owner_id: Optional[int]) -> Optional[int]:
    if owner_id is None:
        return None
    if owner_id not in await teams.members_of(session, SALES):
        raise _refuse(422, "Leads belong to members of the sales team.", "NOT_IN_TEAM", {"field": "owner_id"})
    return owner_id


# ─── Making one ──────────────────────────────────────────────────────────


def _contact_row(lead_id: int, c: dict) -> LeadContacts:
    channel = c.get("preferred_channel") or None
    return LeadContacts(
        lead_id=lead_id, name=(c.get("name") or c.get("email") or "Contact")[:120], role=(c.get("role") or None),
        email=(str(c["email"]).strip().lower() if c.get("email") else None), phone=c.get("phone") or None,
        whatsapp=c.get("whatsapp") or None, preferred_channel=channel, is_primary=bool(c.get("is_primary")),
    )


async def make_lead(
    session, *, name: str, type: str, actor=None, owner_id: Optional[int] = None, city: Optional[str] = None,
    website: Optional[str] = None, source: str = "other", value_inr: Optional[int] = None, expected_close: Optional[date] = None,
    tags: Iterable[str] = (), contacts: Iterable[dict] = (), club_id: Optional[int] = None, case_id: Optional[int] = None,
    stage: str = "new", note: Optional[str] = None, now: Optional[datetime] = None,
) -> Leads:
    """A lead, its contacts and its first timeline line. No duplicate check
    here (the callers make it). The caller commits."""
    now = now or _now()
    if type not in TYPES:
        raise _refuse(422, "Type is club, sponsor, venue, vendor or other.", "INVALID_TYPE", {"field": "type"})
    if source not in SOURCES:
        raise _refuse(422, "Unknown source.", "INVALID_SOURCE", {"field": "source"})
    if stage not in STAGES:
        raise _refuse(422, "Unknown stage.", "INVALID_STAGE", {"field": "stage"})
    contacts = [c for c in contacts if c and (c.get("name") or c.get("email"))][:MAX_CONTACTS]
    clean_name = " ".join((name or "").split())[:200]
    if not clean_name:
        raise _refuse(422, "A lead needs a name.", "INVALID_NAME", {"field": "name"})
    lead = Leads(
        name=clean_name, name_key=key_of(clean_name) or clean_name.lower(), type=type, stage=stage, source=source,
        owner_id=owner_id, city=(" ".join((city or "").split())[:120] or None), city_key=key_of(city) or None,
        website=(website or None), email_domain=lead_domain(website, [c.get("email") for c in contacts]), value_inr=value_inr,
        expected_close=expected_close, tags=clean_tags(tags), club_id=club_id, case_id=case_id, created_by=getattr(actor, "id", None),
        stage_changed_at=now, last_activity_at=now, created_at=now, updated_at=now,
        won_at=now if stage == WON else None, lost_at=now if stage == LOST else None,
    )
    session.add(lead)
    await session.flush()
    for n, c in enumerate(contacts):
        row = _contact_row(lead.id, c)
        row.is_primary = row.is_primary or n == 0
        session.add(row)
    await log(session, lead, "system", note or "Lead added.", actor=actor, details={"source": source}, now=now)
    await session.flush()
    return lead


def clean_tags(values) -> list[str]:
    out: list[str] = []
    for v in values or []:
        t = " ".join(str(v).split()).lower()[:40]
        if t and t not in out:
            out.append(t)
    return out[:20]


async def log(session, lead: Leads, kind: str, body: Optional[str], *, actor=None, occurred_at: Optional[datetime] = None,
              contact_id: Optional[int] = None, file_id: Optional[int] = None, details: Optional[dict] = None,
              provider: Optional[str] = None, provider_message_id: Optional[str] = None, now: Optional[datetime] = None,
              author_name: Optional[str] = None) -> LeadActivities:
    now = now or _now()
    if kind not in ACTIVITY_KINDS:
        raise _refuse(422, "Unknown activity.", "INVALID_ACTIVITY", {"field": "kind"})
    row = LeadActivities(
        lead_id=lead.id, kind=kind, body=body, occurred_at=occurred_at or now, contact_id=contact_id, file_id=file_id,
        author_id=getattr(actor, "id", None), author_name=(author_name or getattr(actor, "full_name", None) or "MyOtherApp")[:120],
        details=details, provider=provider, provider_message_id=provider_message_id, created_at=now,
    )
    session.add(row)
    lead.last_activity_at = max(lead.last_activity_at or now, occurred_at or now)
    lead.updated_at = now
    await session.flush()
    return row


def at_local(day: date, hour: int = 10) -> datetime:
    return datetime.combine(day, time(hour, 0), ZoneInfo(hours.IST)).astimezone(timezone.utc)


async def add_follow_up(session, *, lead: Optional[Leads] = None, case_id: Optional[int] = None, kind: str, title: str,
                        due_at: datetime, owner_id: Optional[int], actor=None) -> FollowUps:
    if kind not in FOLLOW_UP_KINDS:
        raise _refuse(422, "Kind is call, meeting, email, whatsapp, visit or other.", "INVALID_KIND", {"field": "kind"})
    if due_at.tzinfo is None:
        due_at = due_at.replace(tzinfo=timezone.utc)
    f = FollowUps(lead_id=lead.id if lead is not None else None, case_id=case_id, kind=kind, title=" ".join(title.split())[:200] or "Follow up",
                  due_at=due_at, owner_id=owner_id, created_by=getattr(actor, "id", None))
    session.add(f)
    await session.flush()
    return f


async def open_follow_ups(session, lead_id: int, *, exclude: Optional[int] = None) -> int:
    stmt = select(func.count()).select_from(FollowUps).where(FollowUps.lead_id == lead_id, FollowUps.done.is_(False))
    if exclude is not None:
        stmt = stmt.where(FollowUps.id != exclude)
    return int(await session.scalar(stmt) or 0)


async def soon(session, hours_from_now: float = 4.0) -> datetime:
    desk = await hours.get(session)
    return hours.add_working_hours(_now(), hours_from_now, desk["working_hours"])


async def create(session, body, actor, rights: teams.Rights, request=None) -> Leads:
    """POST /admin/leads: by a sales member (theirs) or an owner (anyone's)."""
    rights.require("add leads", sales=True)
    owner_id = body.owner_id if "owner_id" in body.model_fields_set else (actor.id if rights.sales else None)
    if owner_id is not None and owner_id != actor.id and not rights.owner:
        raise _refuse(403, "Only an admin owner can add a lead for someone else.", "OWNER_REQUIRED", {"field": "owner_id"})
    owner_id = await check_owner(session, owner_id)
    contacts = [c.model_dump() for c in (body.contacts or [])]
    if not body.force:
        found = await duplicates(session, body.name, body.city, [lead_domain(body.website, [c.get("email") for c in contacts])])
        if found:
            raise _refuse(409, "This looks like a lead we already have.", "DUPLICATE_LEAD", {"matches": match_out(found)})
    if body.club_id is not None:
        from app.api.clubs.models import Clubs

        if await session.get(Clubs, body.club_id) is None:
            raise _refuse(422, "There's no such club.", "INVALID_CLUB", {"field": "club_id"})
    lead = await make_lead(
        session, name=body.name, type=body.type, actor=actor, owner_id=owner_id, city=body.city, website=body.website,
        source=body.source or "other", value_inr=body.value_inr, expected_close=body.expected_close, tags=body.tags or [],
        contacts=contacts, club_id=body.club_id,
    )
    if body.follow_up is not None:
        fu = body.follow_up
        await add_follow_up(session, lead=lead, kind=fu.kind, title=fu.title, due_at=fu.due_at, owner_id=fu.owner_id or owner_id, actor=actor)
    await audit(session, actor, "create", lead, f"Added the lead {lead.name}", {"type": lead.type, "forced": bool(body.force)}, request)
    return lead


# ─── Changing one ────────────────────────────────────────────────────────


async def lost_reason_ok(session, key: str) -> bool:
    from app.api.support import reference

    await reference.ensure(session)
    return bool(await session.scalar(select(LeadLostReasons.id).where(LeadLostReasons.key == key, LeadLostReasons.active.is_(True))))


async def set_stage(session, lead: Leads, stage: str, actor, *, lost_reason: Optional[str] = None, lost_note: Optional[str] = None,
                    retry_on: Optional[date] = None, renewal_on: Optional[date] = None, now: Optional[datetime] = None) -> bool:
    now = now or _now()
    if stage not in STAGES:
        raise _refuse(422, "Unknown stage.", "INVALID_STAGE", {"field": "stage"})
    if stage == lead.stage:
        return False
    if stage == LOST:
        if not lost_reason or not await lost_reason_ok(session, lost_reason):
            raise _refuse(422, "Say why it was lost.", "LOST_REASON_REQUIRED", {"field": "lost_reason"})
    before = lead.stage
    lead.stage, lead.stage_changed_at, lead.updated_at = stage, now, now
    if stage in (WON, LOST):
        # What was left to do is done with; the try-again follow-up comes after.
        await session.execute(
            update(FollowUps).where(FollowUps.lead_id == lead.id, FollowUps.done.is_(False))
            .values(done=True, done_at=now, done_by=getattr(actor, "id", None)).execution_options(synchronize_session=False)
        )
    if stage == WON:
        lead.won_at, lead.lost_at, lead.lost_reason, lead.lost_note, lead.retry_on = now, None, None, None, None
        if renewal_on is not None:
            lead.renewal_on = renewal_on
    elif stage == LOST:
        lead.lost_at, lead.won_at = now, None
        lead.lost_reason, lead.lost_note, lead.retry_on = lost_reason, (lost_note or None), retry_on
        if retry_on is not None:
            await add_follow_up(session, lead=lead, kind="call", title=f"Try {lead.name} again", due_at=at_local(retry_on),
                                owner_id=lead.owner_id, actor=actor)
    else:
        lead.won_at = lead.lost_at = None
        lead.lost_reason = lead.lost_note = lead.retry_on = None
    await log(session, lead, "stage_change", f"{before} -> {stage}", actor=actor,
              details={"from": before, "to": stage, **({"lost_reason": lost_reason} if stage == LOST else {})}, now=now)
    return True


async def update_lead(session, lead: Leads, body, actor, rights: teams.Rights, request=None) -> dict:
    sent = body.model_fields_set
    changed: list[str] = []
    if "owner_id" in sent and body.owner_id != lead.owner_id:
        taking = rights.sales and lead.owner_id is None and body.owner_id == actor.id
        if not (rights.owner or taking):
            raise _refuse(403, "Only an admin owner can reassign a lead.", "OWNER_REQUIRED")
        lead.owner_id = await check_owner(session, body.owner_id)
        await session.execute(
            update(FollowUps).where(FollowUps.lead_id == lead.id, FollowUps.done.is_(False), FollowUps.owner_id.is_(None))
            .values(owner_id=lead.owner_id).execution_options(synchronize_session=False)
        )
        changed.append("owner_id")
        await log(session, lead, "system", "Owner changed.", actor=actor, details={"owner_id": lead.owner_id})
    rest = sent - {"owner_id"}
    if rest:
        require_edit(rights, lead)
    for field in ("type", "city", "website", "value_inr", "expected_close", "renewal_on"):
        if field in sent and getattr(body, field) != getattr(lead, field):
            value = getattr(body, field)
            if field == "type" and value not in TYPES:
                raise _refuse(422, "Type is club, sponsor, venue, vendor or other.", "INVALID_TYPE", {"field": "type"})
            setattr(lead, field, value)
            changed.append(field)
    if "name" in sent and body.name and body.name != lead.name:
        lead.name = " ".join(body.name.split())[:200]
        lead.name_key = key_of(lead.name) or lead.name.lower()
        changed.append("name")
    if "city" in changed:
        lead.city_key = key_of(lead.city) or None
    if "website" in changed:
        lead.email_domain = await _domain_now(session, lead)
    if "tags" in sent and body.tags is not None:
        lead.tags = clean_tags(body.tags)
        changed.append("tags")
    if "club_id" in sent:
        if body.club_id is not None:
            from app.api.clubs.models import Clubs

            if await session.get(Clubs, body.club_id) is None:
                raise _refuse(422, "There's no such club.", "INVALID_CLUB", {"field": "club_id"})
        lead.club_id = body.club_id
        changed.append("club_id")
    needs = False
    if "stage" in sent and body.stage is not None:
        if await set_stage(session, lead, body.stage, actor, lost_reason=body.lost_reason, lost_note=body.lost_note,
                           retry_on=body.retry_on, renewal_on=body.renewal_on):
            changed.append("stage")
            needs = body.stage in OPEN_STAGES and await open_follow_ups(session, lead.id) == 0
    elif lead.stage == LOST and ("lost_reason" in sent or "retry_on" in sent or "lost_note" in sent):
        if "lost_reason" in sent:
            if not body.lost_reason or not await lost_reason_ok(session, body.lost_reason):
                raise _refuse(422, "Say why it was lost.", "LOST_REASON_REQUIRED", {"field": "lost_reason"})
            lead.lost_reason = body.lost_reason
        if "lost_note" in sent:
            lead.lost_note = body.lost_note
        if "retry_on" in sent and body.retry_on != lead.retry_on:
            lead.retry_on = body.retry_on
            if body.retry_on is not None:
                await add_follow_up(session, lead=lead, kind="call", title=f"Try {lead.name} again", due_at=at_local(body.retry_on),
                                    owner_id=lead.owner_id, actor=actor)
        changed.append("lost")
    if changed:
        lead.updated_at = _now()
        await audit(session, actor, "update", lead, f"Changed the lead {lead.name}: {', '.join(changed)}",
                    {"fields": changed, **({"stage": lead.stage} if "stage" in changed else {})}, request)
    return {"needs_follow_up": needs}


async def _domain_now(session, lead: Leads) -> Optional[str]:
    emails = (await session.scalars(select(LeadContacts.email).where(LeadContacts.lead_id == lead.id))).all()
    return lead_domain(lead.website, emails)


async def merge(session, lead: Leads, into: Leads, actor, now: Optional[datetime] = None) -> Leads:
    """Both timelines kept, in `into`; `lead` points at it and leaves the lists."""
    from app.api.support.models import SupportCases

    now = now or _now()
    if into.id == lead.id:
        raise _refuse(422, "A lead can't be merged into itself.", "SAME_LEAD")
    if into.merged_into_id is not None:
        raise _refuse(422, "That lead was merged into another; merge into that one.", "MERGED_LEAD")
    have = {(e or "").lower() for e in (await session.scalars(select(LeadContacts.email).where(LeadContacts.lead_id == into.id))).all()}
    for c in (await session.scalars(select(LeadContacts).where(LeadContacts.lead_id == lead.id))).all():
        if c.email and c.email.lower() in have:
            await session.delete(c)
        else:
            c.lead_id, c.is_primary = into.id, False
    for model in (LeadActivities, LeadFiles, FollowUps):
        await session.execute(update(model).where(model.lead_id == lead.id).values(lead_id=into.id).execution_options(synchronize_session=False))
    await session.execute(update(SupportCases).where(SupportCases.lead_id == lead.id).values(lead_id=into.id)
                          .execution_options(synchronize_session=False))
    into.tags = clean_tags([*(into.tags or []), *(lead.tags or [])])
    for field in ("club_id", "value_inr", "website", "city", "expected_close", "case_id"):
        if getattr(into, field) is None and getattr(lead, field) is not None:
            setattr(into, field, getattr(lead, field))
    into.city_key = key_of(into.city) or None
    await session.flush()
    into.email_domain = await _domain_now(session, into)
    lead.merged_into_id = into.id
    lead.updated_at = into.updated_at = now
    await log(session, into, "system", f"{lead.name} (lead {lead.id}) was merged into this lead.", actor=actor, now=now)
    await log(session, lead, "system", f"Merged into {into.name} (lead {into.id}).", actor=actor, now=now)
    return into


# ─── Contacts, activities, files, follow-ups ─────────────────────────────


def contact_out(c: LeadContacts) -> dict:
    return {"id": c.id, "name": c.name, "role": c.role, "email": c.email, "phone": c.phone, "whatsapp": c.whatsapp,
            "preferred_channel": c.preferred_channel, "is_primary": c.is_primary}


async def refresh_domain(session, lead: Leads) -> None:
    await session.flush()
    lead.email_domain = await _domain_now(session, lead)


def follow_up_out(f: FollowUps, names: Optional[dict] = None) -> dict:
    return {"id": f.id, "lead_id": f.lead_id, "case_id": f.case_id, "kind": f.kind, "title": f.title, "due_at": f.due_at,
            "owner": {"id": f.owner_id, "name": (names or {}).get(f.owner_id)} if f.owner_id else None, "done": f.done,
            "done_at": f.done_at, "overdue": (not f.done) and f.due_at < _now()}


async def store_file(session, lead: Leads, data: bytes, content_type: str, filename: str, actor=None) -> LeadFiles:
    from app.api.support import store

    key = store.new_key("leads", lead.id, filename)
    await asyncio.to_thread(store.put, key, data, content_type)
    row = LeadFiles(lead_id=lead.id, storage_key=key, filename=filename, content_type=content_type, size_bytes=len(data),
                    uploaded_by=getattr(actor, "id", None))
    session.add(row)
    await session.flush()
    return row


# ─── Reading ─────────────────────────────────────────────────────────────


async def get_lead(session, lead_id: int, *, lock: bool = False) -> Leads:
    stmt = select(Leads).where(Leads.id == int(lead_id))
    if lock:
        stmt = stmt.with_for_update()
    lead = await session.scalar(stmt.execution_options(populate_existing=True))
    if lead is None:
        raise _refuse(404, "There's no such lead.", "LEAD_NOT_FOUND")
    return lead


async def next_follow_ups(session, lead_ids: list[int]) -> dict[int, FollowUps]:
    if not lead_ids:
        return {}
    rows = (await session.scalars(
        select(FollowUps).where(FollowUps.lead_id.in_(lead_ids), FollowUps.done.is_(False))
        .order_by(FollowUps.lead_id, FollowUps.due_at, FollowUps.id).distinct(FollowUps.lead_id)
    )).all()
    return {f.lead_id: f for f in rows}


async def contacts_counts(session, lead_ids: list[int]) -> dict[int, int]:
    if not lead_ids:
        return {}
    return dict((await session.execute(
        select(LeadContacts.lead_id, func.count()).where(LeadContacts.lead_id.in_(lead_ids)).group_by(LeadContacts.lead_id)
    )).all())


def row_out(lead: Leads, names: dict, nxt: Optional[FollowUps], contacts: int, now: datetime) -> dict:
    return {
        "id": lead.id, "name": lead.name, "type": lead.type, "stage": lead.stage, "city": lead.city, "source": lead.source,
        "owner": {"id": lead.owner_id, "name": names.get(lead.owner_id)} if lead.owner_id else None,
        "value_inr": lead.value_inr, "expected_close": lead.expected_close, "tags": list(lead.tags or []), "club_id": lead.club_id,
        "next_follow_up": {"id": nxt.id, "due_at": nxt.due_at, "kind": nxt.kind, "title": nxt.title} if nxt else None,
        "overdue": bool(nxt and nxt.due_at < now), "contacts_count": int(contacts or 0), "updated_at": lead.updated_at,
    }


def list_filters(*, user_id: int, type=None, stage=None, owner=None, city=None, due=None, q=None, now=None) -> list:
    L = Leads
    now = now or _now()
    out = [L.merged_into_id.is_(None)]
    if type:
        out.append(L.type == type)
    if stage:
        out.append(L.stage == stage)
    if owner == "me":
        out.append(L.owner_id == user_id)
    elif owner == "none":
        out.append(L.owner_id.is_(None))
    elif owner not in (None, ""):
        try:
            out.append(L.owner_id == int(owner))
        except (TypeError, ValueError):
            raise _refuse(422, "owner is an admin's id, me or none.", "INVALID_FILTER", {"field": "owner"}) from None
    if city:
        out.append(L.city.ilike(f"%{city.replace('%', '')}%"))
    if due:
        start, end = hours.today_bounds(now)
        windows = {"today": (FollowUps.due_at >= start, FollowUps.due_at < end), "overdue": (FollowUps.due_at < now,),
                   "week": (FollowUps.due_at < start + timedelta(days=7),)}
        if due not in windows:
            raise _refuse(422, "due is today, overdue or week.", "INVALID_FILTER", {"field": "due"})
        out.append(exists().where(FollowUps.lead_id == L.id, FollowUps.done.is_(False), *windows[due]))
    words = (q or "").strip()
    if words:
        like = f"%{words.replace('%', '').replace('_', '')}%"
        out.append(or_(
            L.name.ilike(like), L.city.ilike(like), L.website.ilike(like),
            exists().where(LeadContacts.lead_id == L.id, or_(LeadContacts.name.ilike(like), LeadContacts.email.ilike(like))),
        ))
    return out


#: The table's orders besides its own (latest activity first): the column clicked.
LEAD_SORTS = ("name", "type", "stage", "city", "value", "close", "created", "updated")


def lead_order(sort: Optional[str], order: Optional[str]) -> Optional[tuple]:
    """ORDER BY for a column clicked (None: the view's own). Blanks last; id breaks ties."""
    if sort not in LEAD_SORTS:
        return None
    L = Leads
    column = {
        "name": func.lower(L.name), "type": L.type, "stage": L.stage, "city": func.lower(L.city),
        "value": L.value_inr, "close": L.expected_close, "created": L.created_at, "updated": L.updated_at,
    }[sort]
    descending = order == "desc" if order in ("asc", "desc") else sort in ("value", "created", "updated")
    return (column.desc().nulls_last(), L.id.desc()) if descending else (column.asc().nulls_last(), L.id.asc())


async def list_leads(
    session, user_id: int, *, view: str = "table", page: int = 1, page_size: int = 50,
    sort: Optional[str] = None, order: Optional[str] = None, **f,
) -> dict:
    now = _now()
    L = Leads
    where = list_filters(user_id=user_id, now=now, **f)
    total = int(await session.scalar(select(func.count()).select_from(L).where(*where)) or 0)
    no_stage = list_filters(user_id=user_id, now=now, **{**f, "stage": None})
    by_stage = {s: 0 for s in STAGES}
    for s, n in (await session.execute(select(L.stage, func.count()).where(*no_stage).group_by(L.stage))).all():
        by_stage[s] = int(n)
    clicked = lead_order(sort, order) if view == "table" else None
    order = clicked or ((L.stage, L.updated_at.desc(), L.id.desc()) if view == "board" else (L.updated_at.desc(), L.id.desc()))
    rows = (await session.scalars(select(L).where(*where).order_by(*order).limit(page_size).offset((page - 1) * page_size))).all()
    ids = [r.id for r in rows]
    names = await teams.names(session, [r.owner_id for r in rows])
    nxt = await next_follow_ups(session, ids)
    counts = await contacts_counts(session, ids)
    return {"items": [row_out(r, names, nxt.get(r.id), counts.get(r.id, 0), now) for r in rows], "total": total,
            "by_stage": by_stage, "page": page, "page_size": page_size, "view": view}


async def export_rows(session, user_id: int, **f):
    now = _now()
    L = Leads
    where = list_filters(user_id=user_id, now=now, **f)
    yield ["Id", "Name", "Type", "Stage", "City", "Source", "Owner", "Value (INR a year)", "Expected close", "Next follow-up",
           "Next follow-up due", "Overdue", "Contacts", "Primary contact", "Primary email", "Primary phone", "Tags", "Updated"]
    last = None
    while True:
        stmt = select(L).where(*where).order_by(L.id.desc()).limit(500)
        if last is not None:
            stmt = stmt.where(L.id < last)
        rows = (await session.scalars(stmt)).all()
        if not rows:
            break
        ids = [r.id for r in rows]
        names = await teams.names(session, [r.owner_id for r in rows])
        nxt = await next_follow_ups(session, ids)
        counts = await contacts_counts(session, ids)
        primary = {c.lead_id: c for c in (await session.scalars(
            select(LeadContacts).where(LeadContacts.lead_id.in_(ids)).order_by(LeadContacts.lead_id, LeadContacts.is_primary.desc(), LeadContacts.id)
            .distinct(LeadContacts.lead_id)
        )).all()}
        for r in rows:
            n, c = nxt.get(r.id), primary.get(r.id)
            yield [r.id, r.name, r.type, r.stage, r.city or "", r.source, names.get(r.owner_id, ""), r.value_inr, r.expected_close,
                   n.title if n else "", n.due_at if n else None, bool(n and n.due_at < now), counts.get(r.id, 0),
                   c.name if c else "", c.email if c else "", c.phone if c else "", ", ".join(r.tags or []), r.updated_at]
        last = rows[-1].id


async def linked(session, lead: Leads) -> dict:
    """The platform's own records a lead is about."""
    from app.api.clubs.models import ClubPartners, Clubs
    from app.api.event_partners.models import EventPartners
    from app.api.events.models import Events
    from app.api.subscriptions.models import ClubSubscriptions, Plans

    out: dict = {"club": None, "event_partners": [], "club_partners": [], "events_at_venue": []}
    if lead.club_id:
        club = await session.get(Clubs, lead.club_id)
        if club is not None:
            sub = (await session.execute(
                select(ClubSubscriptions, Plans).join(Plans, Plans.id == ClubSubscriptions.plan_id)
                .where(ClubSubscriptions.club_id == club.id).order_by(ClubSubscriptions.created_at.desc()).limit(1)
            )).first()
            out["club"] = {
                "id": club.id, "name": club.name, "slug": club.slug, "verified": bool(club.is_verified), "path": f"/clubs/{club.id}",
                "subscription": {
                    "plan": sub[1].key, "plan_name": sub[1].name, "status": getattr(sub[0].status, "value", sub[0].status),
                    "cycle": getattr(sub[0].cycle, "value", sub[0].cycle), "current_period_end": sub[0].current_period_end,
                } if sub else None,
            }
    name = lead.name.strip().lower()
    if lead.type in ("sponsor", "vendor", "other"):
        rows = (await session.execute(
            select(EventPartners, Events.name).join(Events, Events.id == EventPartners.event_id)
            .where(func.lower(EventPartners.name) == name).order_by(EventPartners.id.desc()).limit(20)
        )).all()
        out["event_partners"] = [{"id": p.id, "event_id": p.event_id, "event_name": ev, "kind": p.kind, "path": f"/events/{p.event_id}"}
                                 for p, ev in rows]
        rows = (await session.execute(
            select(ClubPartners, Clubs.name).join(Clubs, Clubs.id == ClubPartners.club_id)
            .where(func.lower(ClubPartners.name) == name).limit(20)
        )).all()
        out["club_partners"] = [{"id": p.id, "club_id": p.club_id, "club_name": cn, "path": f"/clubs/{p.club_id}"} for p, cn in rows]
    if lead.type == "venue" and len(name) >= 3:
        rows = (await session.execute(
            select(Events.id, Events.name, Events.event_datetime).where(Events.location_name.ilike(f"%{name.replace('%', '')}%"))
            .order_by(Events.event_datetime.desc()).limit(20)
        )).all()
        out["events_at_venue"] = [{"id": i, "name": n, "event_datetime": d, "path": f"/events/{i}"} for i, n, d in rows]
    return out


async def detail(session, lead: Leads, rights: teams.Rights) -> dict:
    now = _now()
    names = await teams.names(session, [lead.owner_id])
    nxt = (await next_follow_ups(session, [lead.id])).get(lead.id)
    contacts = (await session.scalars(
        select(LeadContacts).where(LeadContacts.lead_id == lead.id).order_by(LeadContacts.is_primary.desc(), LeadContacts.id)
    )).all()
    out = row_out(lead, names, nxt, len(contacts), now)
    acts = (await session.scalars(
        select(LeadActivities).where(LeadActivities.lead_id == lead.id).order_by(LeadActivities.occurred_at.desc(), LeadActivities.id.desc())
        .limit(500)
    )).all()
    lead_files = (await session.scalars(select(LeadFiles).where(LeadFiles.lead_id == lead.id).order_by(LeadFiles.id.desc()))).all()
    fus = (await session.scalars(
        select(FollowUps).where(FollowUps.lead_id == lead.id).order_by(FollowUps.done, FollowUps.due_at, FollowUps.id)
    )).all()
    fu_names = await teams.names(session, [f.owner_id for f in fus])
    out.update({
        "website": lead.website, "email_domain": lead.email_domain, "lost_reason": lead.lost_reason, "lost_note": lead.lost_note,
        "retry_on": lead.retry_on, "won_at": lead.won_at, "lost_at": lead.lost_at, "renewal_on": lead.renewal_on,
        "case_id": lead.case_id, "merged_into_id": lead.merged_into_id, "created_at": lead.created_at,
        "stage_changed_at": lead.stage_changed_at, "last_activity_at": lead.last_activity_at,
        "contacts": [contact_out(c) for c in contacts],
        "timeline": [{"id": a.id, "kind": a.kind, "body": a.body, "occurred_at": a.occurred_at, "contact_id": a.contact_id,
                      "file_id": a.file_id, "author_id": a.author_id, "author_name": a.author_name, "details": a.details} for a in acts],
        "files": [{"id": f.id, "name": f.filename, "size": f.size_bytes, "content_type": f.content_type, "created_at": f.created_at,
                   "url": f"/api/v1/admin/leads/{lead.id}/files/{f.id}"} for f in lead_files],
        "follow_ups": [follow_up_out(f, fu_names) for f in fus],
        "linked": await linked(session, lead), "can_edit": can_edit(rights, lead),
        "can_take": rights.sales and lead.owner_id is None,
    })
    return out


# ─── The other doors ─────────────────────────────────────────────────────


async def talk_to_us(session, body, request=None) -> Leads:
    """The For-clubs page's form: a lead (or a line on the one we have), its
    person, and a follow-up for the sales team's round robin."""
    now = _now()
    email = str(body.email).strip().lower()
    domain = domain_of(email)
    found = await duplicates(session, body.organisation, body.city, [domain])
    message = (body.message or "").strip()
    note = f"Talk to us: {message}" if message else "Talk to us (no message)."
    details = {"page_url": body.page_url, "type": body.type, "name": body.name, "phone": body.phone}
    due = await soon(session, 4.0)
    if found:
        lead = found[0]
        has = await session.scalar(select(LeadContacts.id).where(LeadContacts.lead_id == lead.id, func.lower(LeadContacts.email) == email))
        if has is None:
            session.add(_contact_row(lead.id, {"name": body.name, "email": email, "phone": body.phone}))
            await refresh_domain(session, lead)
        await log(session, lead, "note", note, author_name=body.name, details=details, now=now)
        owner = lead.owner_id or await teams.next_member(session, SALES, now)
        await add_follow_up(session, lead=lead, kind="email", title=f"Answer {body.name} (Talk to us)", due_at=due, owner_id=owner)
        await audit(session, None, "talk_to_us", lead, f"Talk to us: {body.organisation} wrote again", {"existing": True}, request)
        return lead
    owner = await teams.next_member(session, SALES, now)
    lead = await make_lead(
        session, name=body.organisation, type=body.type, owner_id=owner, city=body.city, source="for_clubs",
        contacts=[{"name": body.name, "email": email, "phone": body.phone}], note="From the Talk to us form.", now=now,
    )
    await log(session, lead, "note", note, author_name=body.name, details=details, now=now)
    await add_follow_up(session, lead=lead, kind="email", title=f"Answer {body.name} (Talk to us)", due_at=due, owner_id=owner)
    await audit(session, None, "talk_to_us", lead, f"Talk to us: {body.organisation}", {"existing": False}, request)
    return lead


async def on_club_signup(session, club) -> Optional[Leads]:
    """A club that applied by itself, as a Club lead to track its onboarding
    (or a line on the lead we already had for it). Never fails the sign-up:
    the caller wraps this, and logs."""
    now = _now()
    from app.api.users.models import Users

    account = await session.get(Users, club.user_id) if club.user_id else None
    email = (club.contact_email or getattr(account, "email", None) or "").strip().lower() or None
    found = list((await session.scalars(select(Leads).where(Leads.club_id == club.id, Leads.merged_into_id.is_(None)).limit(1))).all())
    if not found:
        found = await duplicates(session, club.name, club.location_name, [domain_of(email)])
    if found:
        lead = found[0]
        if lead.club_id is None:
            lead.club_id = club.id
        await log(session, lead, "system", f"{club.name} applied for a club account by itself.", details={"club_id": club.id}, now=now)
        await audit(session, None, "club_signup", lead, f"{club.name} applied: added to its lead", {"club_id": club.id})
        return lead
    owner = await teams.next_member(session, SALES, now)
    lead = await make_lead(
        session, name=club.name, type="club", owner_id=owner, city=(club.location_name or "")[:120] or None, source="signup",
        contacts=[{"name": club.name, "email": email, "phone": club.contact_phone}] if email or club.contact_phone else [],
        club_id=club.id, note=f"{club.name} applied for a club account by itself.", now=now,
    )
    desk = await hours.get(session)
    due = hours.add_working_hours(now, 9.0, desk["working_hours"])
    await add_follow_up(session, lead=lead, kind="call", title=f"Help {club.name} get started", due_at=due, owner_id=owner)
    await audit(session, None, "club_signup", lead, f"{club.name} applied: a club lead", {"club_id": club.id})
    return lead


async def from_case(session, case, type: str, actor, rights: teams.Rights, *, name: Optional[str] = None, force: bool = False) -> Leads:
    """A support case turned into a lead (the support team, sales, owners)."""
    from app.api.clubs.models import Clubs

    if case.lead_id:
        lead = await session.get(Leads, case.lead_id)
        if lead is not None:
            raise _refuse(409, "This case is a lead already.", "ALREADY_A_LEAD", {"lead_id": lead.id})
    club = await session.get(Clubs, case.requester_club_id) if case.requester_club_id else None
    lead_name = (name or (club.name if club else None) or case.requester_name or domain_of(case.requester_email) or f"Case {case.number}")
    contacts = [{"name": case.requester_name or case.requester_email, "email": case.requester_email}] if case.requester_email else []
    if not force:
        found = await duplicates(session, lead_name, None, [domain_of(case.requester_email)])
        if found:
            raise _refuse(409, "This looks like a lead we already have.", "DUPLICATE_LEAD", {"matches": match_out(found)})
    owner = actor.id if rights.sales else await teams.next_member(session, SALES)
    lead = await make_lead(
        session, name=lead_name, type=type, actor=actor, owner_id=owner, source="other", contacts=contacts,
        club_id=club.id if club else None, case_id=case.id, note=f"From support case #{case.number}: {case.subject}",
    )
    case.lead_id = lead.id
    await audit(session, actor, "from_case", lead, f"Lead {lead.name} from case #{case.number}", {"case_id": case.id})
    return lead
