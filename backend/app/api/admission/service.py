"""How an event numbers its tickets: a prefix, a padded sequence, and the
event code that keeps two events apart. Numbers already given out are never
rewritten; the format applies to the next ticket minted."""
import re

from sqlalchemy import func, select, text, update

from app.api.events.models import EventRegistrationsLink, Events
from app.core.layout.admission import ADMISSION_PREFIX_DEFAULT, admission_number
from app.response import CustomHTTPException

PREFIX = re.compile(r"^[A-Z0-9]+(-[A-Z0-9]+)*$")


def event_code(event: Events) -> str:
    return f"E{event.id}"


def format_of(event: Events) -> dict:
    return {"prefix": event.admission_prefix, "digits": event.admission_digits or 4, "includeCode": bool(event.admission_include_code) if event.admission_include_code is not None else True}


def number_for(event: Events, seq: int) -> str:
    return admission_number(format_of(event), event_code(event), seq)


async def next_seq(session, event_id: int) -> int:
    """One more than the last given, taken atomically so two registrations never share a number."""
    row = (await session.execute(update(Events).where(Events.id == event_id).values(admission_seq=func.coalesce(Events.admission_seq, 0) + 1).returning(Events.admission_seq))).first()
    return int(row[0]) if row else 1


async def assign(session, event: Events, registration: EventRegistrationsLink) -> str:
    """The next number for this registration; a no-op when it has one."""
    if registration.admission_no:
        return registration.admission_no
    seq = await next_seq(session, event.id)
    registration.admission_no = number_for(event, seq)
    return registration.admission_no


async def settings(session, event: Events) -> dict:
    numbered = await session.scalar(select(func.count()).select_from(EventRegistrationsLink).where(EventRegistrationsLink.event_id == event.id, EventRegistrationsLink.is_deleted == False, EventRegistrationsLink.admission_no.isnot(None))) or 0  # noqa: E712
    total = await session.scalar(select(func.count()).select_from(EventRegistrationsLink).where(EventRegistrationsLink.event_id == event.id, EventRegistrationsLink.is_deleted == False)) or 0  # noqa: E712
    fmt = format_of(event)
    return {
        "prefix": event.admission_prefix, "digits": fmt["digits"], "include_code": fmt["includeCode"], "code": event_code(event),
        "next_number": number_for(event, (event.admission_seq or 0) + 1), "numbered": numbered, "unnumbered": max(0, total - numbered),
    }


async def update_settings(session, event: Events, body) -> dict:
    data = body.model_dump(exclude_unset=True)
    if "prefix" in data:
        raw = (data["prefix"] or "").strip().upper()
        if raw and not PREFIX.match(raw):
            raise CustomHTTPException(422, message="Letters, digits and single dashes, twelve characters at most.", errors={"fields": {"prefix": "Letters, digits and single dashes."}})
        event.admission_prefix = raw or None
    if data.get("digits") is not None:
        event.admission_digits = data["digits"]
    if data.get("include_code") is not None:
        event.admission_include_code = data["include_code"]
    await session.commit()
    return await settings(session, event)


#: admission_number() (app/core/layout/admission.py) as SQL, for a sequence
#: computed in the statement: PREFIX-0042[-E123]. lpad() would cut a
#: sequence longer than the digits, so a long one is left whole, as zfill
#: leaves it.
SQL_NUMBER = """
  coalesce(nullif(:prefix, ''), :default_prefix) || '-' ||
  CASE WHEN length(s.seq::text) >= :digits THEN s.seq::text ELSE lpad(s.seq::text, :digits, '0') END ||
  CASE WHEN :include_code THEN '-' || :code ELSE '' END
"""

#: Everyone unnumbered, in the order they came; the event's sequence moved
#: once by how many that is; each row its number from its place in line.
NUMBER_ALL = f"""
WITH todo AS (
  SELECT id, row_number() OVER (ORDER BY created_at, id) AS n
  FROM event_registrations_link
  WHERE event_id = :event_id AND NOT is_deleted AND admission_no IS NULL
), bump AS (
  UPDATE events SET admission_seq = coalesce(admission_seq, 0) + (SELECT count(*) FROM todo)
  WHERE id = :event_id
  RETURNING admission_seq - (SELECT count(*) FROM todo) AS before
), s AS (
  SELECT todo.id, todo.n, bump.before + todo.n AS seq FROM todo, bump
)
UPDATE event_registrations_link r SET admission_no = {SQL_NUMBER}
FROM s WHERE r.id = s.id
RETURNING s.n, r.admission_no
"""


async def number_all(session, event: Events) -> dict:
    """Everyone registered so far who has no number yet, in the order they
    came: one statement. The event's sequence moves once, by however many
    there are, and every row takes its place from row_number() -- where
    this used to hold the event's row through an UPDATE per registration.

    Registrations arriving meanwhile wait on the event's row as they would
    for any other registration, then number after these."""
    fmt = format_of(event)
    rows = sorted((await session.execute(text(NUMBER_ALL), {
        "event_id": event.id, "prefix": fmt["prefix"] or "", "default_prefix": ADMISSION_PREFIX_DEFAULT,
        "digits": int(fmt["digits"]), "include_code": bool(fmt["includeCode"]), "code": event_code(event),
    })).all())
    await session.commit()
    await session.refresh(event)
    return {"numbered": len(rows), "first": rows[0][1] if rows else None, "last": rows[-1][1] if rows else None}


def default_prefix() -> str:
    return ADMISSION_PREFIX_DEFAULT

