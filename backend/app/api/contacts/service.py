"""The club's contact book: what retention kept of each event's guests."""
import csv
import io
from typing import Optional

from sqlalchemy import func, or_, select

from app.api.closeout.models import ArchivedContacts


def _base(club_id: int):
    return select(ArchivedContacts).where(ArchivedContacts.club_id == club_id)


def _filtered(club_id: int, *, q: Optional[str], event_id: Optional[int], attended: Optional[bool]):
    s = _base(club_id)
    if q:
        pat = f"%{q.strip()}%"
        s = s.where(or_(ArchivedContacts.name.ilike(pat), ArchivedContacts.email.ilike(pat), ArchivedContacts.phone.ilike(pat), ArchivedContacts.event_name.ilike(pat)))
    if event_id is not None:
        s = s.where(ArchivedContacts.event_id == event_id)
    if attended is not None:
        s = s.where(ArchivedContacts.attended == attended)
    return s


def contact_out(c: ArchivedContacts) -> dict:
    return {
        "id": c.id, "name": c.name, "email": c.email, "phone": c.phone, "role": c.role, "attended": c.attended,
        "event_id": c.event_id, "event_name": c.event_name, "event_ended_at": c.event_ended_at, "archived_at": c.created_at,
    }


async def groups(session, club_id: int) -> list:
    """Contacts arrive in groups, one event's retention run each; a group is the unit the organiser filters by."""
    rows = (await session.execute(
        select(ArchivedContacts.event_id, ArchivedContacts.event_name, func.max(ArchivedContacts.event_ended_at), func.max(ArchivedContacts.created_at), func.count())
        .where(ArchivedContacts.club_id == club_id).group_by(ArchivedContacts.event_id, ArchivedContacts.event_name).order_by(func.max(ArchivedContacts.created_at).desc())
    )).all()
    return [{"event_id": eid, "event_name": name, "event_ended_at": ended, "archived_at": archived, "contacts": n} for eid, name, ended, archived, n in rows]


async def page(session, club_id: int, *, q: Optional[str] = None, event_id: Optional[int] = None, attended: Optional[bool] = None, page: int = 1, page_size: int = 50) -> dict:
    s = _filtered(club_id, q=q, event_id=event_id, attended=attended)
    total = await session.scalar(select(func.count()).select_from(s.subquery())) or 0
    page = max(1, page)
    page_size = max(1, min(page_size, 200))
    rows = list((await session.scalars(s.order_by(ArchivedContacts.created_at.desc(), ArchivedContacts.name).limit(page_size).offset((page - 1) * page_size))).all())
    return {"items": [contact_out(c) for c in rows], "total": total, "page": page, "page_size": page_size, "groups": await groups(session, club_id)}


async def export_csv(session, club_id: int, *, q: Optional[str] = None, event_id: Optional[int] = None, attended: Optional[bool] = None) -> str:
    """A table as a file: CSV opens anywhere."""
    rows = list((await session.scalars(_filtered(club_id, q=q, event_id=event_id, attended=attended).order_by(ArchivedContacts.event_name, ArchivedContacts.name))).all())
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Name", "Email", "Phone", "Role", "Attended", "Event", "Event ended", "Kept on"])
    for c in rows:
        w.writerow([c.name, c.email or "", c.phone or "", c.role, "yes" if c.attended else "no", c.event_name, c.event_ended_at.isoformat() if c.event_ended_at else "", c.created_at.isoformat() if c.created_at else ""])
    return out.getvalue()
