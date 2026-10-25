"""The registrations export: an .xlsx of an event's guest list, written as
the rows arrive.

The rows come off a server-side cursor a batch at a time, and each batch is
written by openpyxl's write-only workbook on a worker thread, into a
temporary file the response then streams. It used to load every
registration (with its account, profile and organisation) into memory,
build a pandas DataFrame and write the workbook on the event loop, which
stalled every other request for as long as a big event took.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from decimal import Decimal

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.api.events.access import ensure_event_access
from app.api.events.models import EventRegistrationsLink as R
from app.api.events.models import Events
from app.api.events.volunteer.models import Volunteer
from app.response import CustomHTTPException

#: Rows per fetch from the cursor, and per hand-off to the writer thread.
BATCH = 2000
SHEET = "Event Registrations"
LEAD = ["Ticket ID", "Full Name", "Email", "Phone"]
TAIL = ["Is Paid", "Actual Amount", "Paid Amount", "Is Attended", "Checked In By", "Registered At"]


async def _answer_keys(session: AsyncSession, event: Events, where) -> list[str]:
    """The answers' columns: the event's questions in their order, then any
    other key an answer carries (an import's, the organisation), by when it
    first came. Counted by the database, off the same rows."""
    asked = [q.get("key") for q in (event.additional_details or []) if isinstance(q, dict) and q.get("key")]
    # A function in FROM sees the rows before it: one key per row per answer.
    key = func.json_object_keys(R.additional_details).column_valued("answer_key")
    rows = (
        await session.execute(
            select(key)
            .select_from(R)
            .where(*where, func.json_typeof(R.additional_details) == "object")
            .group_by(key)
            .order_by(func.min(R.created_at), key)
        )
    ).scalars().all()
    taken = set(LEAD) | set(TAIL)
    out = [k for k in asked if k not in taken]
    out += [k for k in rows if k not in taken and k not in out]
    return out


def _cell_value(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, list):
        return ", ".join("" if v is None else str(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return value


class _Writer:
    """openpyxl's write-only workbook. Every call runs on a worker thread,
    one at a time."""

    def __init__(self, header: list[str]):
        self.book = Workbook(write_only=True)
        self.sheet = self.book.create_sheet(SHEET)
        self.sheet.append(header)

    def _cell(self, value):
        value = _cell_value(value)
        if isinstance(value, str) and value.startswith("="):
            # A guest's answer is text, never a formula the sheet would run.
            cell = WriteOnlyCell(self.sheet, value=value)
            cell.data_type = "s"
            return cell
        return value

    def append(self, rows: list[list]) -> None:
        for row in rows:
            self.sheet.append([self._cell(v) for v in row])

    def save(self) -> str:
        fd, path = tempfile.mkstemp(prefix="registrations-", suffix=".xlsx")
        os.close(fd)
        self.book.save(path)
        return path


async def write_export(
    session: AsyncSession, user_id: int, event_id: int, *,
    is_attended: bool | None = None, is_paid: bool | None = None, as_admin: bool = False,
) -> str:
    """The event's registrations as an .xlsx file on disk; the caller sends
    it and removes it. Owner (or admin) only."""
    event = await session.scalar(select(Events).where(Events.id == event_id).options(joinedload(Events.club)))
    if event is None:
        raise CustomHTTPException(404, message="Event not found")
    ensure_event_access(event, user_id, as_admin)

    where = [R.event_id == event_id, R.is_deleted == False]  # noqa: E712
    if is_attended is not None:
        where.append(R.is_attended == is_attended)
    if is_paid is not None:
        where.append(R.is_paid == is_paid)
    answer_keys = await _answer_keys(session, event, where)
    writer = await asyncio.to_thread(_Writer, LEAD + answer_keys + TAIL)

    stmt = (
        select(
            R.ticket_id, R.full_name, R.email, R.phone, R.additional_details,
            R.is_paid, R.actual_amount, R.paid_amount, R.is_attended, Volunteer.email, R.created_at,
        )
        .select_from(R)
        .outerjoin(Volunteer, and_(Volunteer.id == R.volunteer_id, Volunteer.is_deleted == False))  # noqa: E712
        .where(*where)
        .order_by(func.lower(R.full_name), R.id)
        .execution_options(yield_per=BATCH)
    )
    result = await session.stream(stmt)
    async for batch in result.partitions(BATCH):
        rows = []
        for ticket_id, name, email, phone, answers, is_paid, actual, paid, attended, volunteer, created in batch:
            answers = answers if isinstance(answers, dict) else {}
            rows.append([
                ticket_id, name, email, phone,
                *(answers.get(k) for k in answer_keys),
                is_paid, actual, paid, attended, volunteer,
                created.strftime("%Y-%m-%d %H:%M:%S") if created else None,
            ])
        await asyncio.to_thread(writer.append, rows)
    return await asyncio.to_thread(writer.save)
