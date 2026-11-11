"""Leads from a sheet: an Excel workbook (.xlsx) or a CSV, one lead a row.

A dry run reads every row and says what would happen to it -- `new`,
`duplicate` (of a lead we have, or of a row above it) or `error` with the
reason -- and changes nothing. Without it the `new` rows are added (source
`import`); duplicates and errors are left out. The owner is the person
importing unless a row names a sales team member by `owner_email` (an owner
importing may assign by city or type that way).

The columns are COLUMNS below, and the template (GET /admin/leads/import/template)
holds them with a sheet explaining each.
"""
from __future__ import annotations

import csv
import io
from datetime import date, datetime
from typing import Optional

from sqlalchemy import func, select

from app.api.leads import service
from app.api.leads.models import SOURCES, TYPES
from app.api.support import teams
from app.api.support.models import SALES
from app.response import CustomHTTPException

MAX_ROWS = 2000
MAX_BYTES = 5 * 1024 * 1024
COLUMNS: list[tuple[str, str]] = [
    ("name", "Required. The organisation's name."),
    ("type", "Required. club, sponsor, venue, vendor or other."),
    ("city", "Its city."),
    ("website", "Its website."),
    ("source", f"How we know them: {', '.join(SOURCES)}. Empty: import."),
    ("value_inr", "Estimated value, rupees a year (a whole number)."),
    ("expected_close", "When we expect to close, YYYY-MM-DD."),
    ("tags", "Words to find it by, separated by commas."),
    ("contact_name", "The person we talk to."),
    ("contact_role", "Their role."),
    ("contact_email", "Their email (also used for the duplicate check)."),
    ("contact_phone", "Their phone."),
    ("contact_whatsapp", "Their WhatsApp number."),
    ("owner_email", "A sales team member's email to own it. Empty: whoever imports."),
    ("note", "A first line for its timeline."),
]
EXAMPLE = ["Robotics Guild, NIT Calicut", "club", "Kozhikode", "https://robotics.example.org", "outreach", "12000", "2026-12-31",
           "engineering, kerala", "Ananya Iyer", "Secretary", "ananya@robotics.example.org", "+91 98470 00000", "", "", "Met at DevFest"]


def _refuse(message: str, code: str = "INVALID_SHEET") -> CustomHTTPException:
    return CustomHTTPException(422, message=message, error_code=code)


def _rows_from(data: bytes, filename: str) -> list[dict]:
    name = (filename or "").lower()
    if data[:2] == b"PK" or name.endswith(".xlsx"):
        from openpyxl import load_workbook

        try:
            wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        except Exception:  # noqa: BLE001
            raise _refuse("That workbook couldn't be read. Save it as .xlsx or CSV and try again.") from None
        ws = wb.worksheets[0]
        values = list(ws.iter_rows(values_only=True, max_row=MAX_ROWS + 2))
        wb.close()
    else:
        try:
            textual = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            textual = data.decode("latin-1")
        values = list(csv.reader(io.StringIO(textual)))[: MAX_ROWS + 2]
    if not values:
        raise _refuse("The sheet is empty.")
    header = [str(h or "").strip().lower().replace(" ", "_") for h in values[0]]
    if "name" not in header or "type" not in header:
        raise _refuse("The first row must name the columns; name and type are required (see the template).")
    if len(values) - 1 > MAX_ROWS:
        raise _refuse(f"At most {MAX_ROWS} rows at a time.", "TOO_MANY_ROWS")
    out = []
    for n, row in enumerate(values[1:], start=2):
        cells = {header[i]: row[i] for i in range(min(len(header), len(row))) if header[i]}
        if not any(v not in (None, "") for v in cells.values()):
            continue
        out.append({"row": n, **cells})
    return out


def _text(value) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    s = " ".join(str(value).split())
    return s or None


def _date(value) -> Optional[date]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        raise ValueError("expected_close is YYYY-MM-DD") from None


def _int(value) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        n = int(float(str(value).replace(",", "").replace("₹", "").strip()))
    except ValueError:
        raise ValueError("value_inr is a whole number of rupees") from None
    if n < 0:
        raise ValueError("value_inr can't be negative")
    return n


async def run(session, data: bytes, filename: str, *, dry_run: bool, actor, rights: teams.Rights) -> dict:
    rights.require("import leads", sales=True)
    if len(data) > MAX_BYTES:
        raise CustomHTTPException(413, message="The sheet can be 5 MB at most.", error_code="FILE_TOO_LARGE")
    rows = _rows_from(data, filename)
    from app.api.users.models import Users

    sales_ids = set(await teams.members_of(session, SALES))
    owners: dict[str, int] = {}
    if sales_ids:
        for uid, email in (await session.execute(select(Users.id, func.lower(Users.email)).where(Users.id.in_(sales_ids)))).all():
            owners[email] = uid
    seen_names: dict[tuple, int] = {}
    seen_domains: dict[str, int] = {}
    results, created = [], 0
    for r in rows:
        name, typ = _text(r.get("name")), (_text(r.get("type")) or "").lower()
        city, email = _text(r.get("city")), (_text(r.get("contact_email")) or "").lower() or None
        item = {"row": r["row"], "name": name, "type": typ or None, "city": city, "email": email}
        try:
            if not name:
                raise ValueError("name is required")
            if typ not in TYPES:
                raise ValueError("type is club, sponsor, venue, vendor or other")
            source = (_text(r.get("source")) or "import").lower()
            if source not in SOURCES:
                raise ValueError(f"source is one of {', '.join(SOURCES)}")
            if email and "@" not in email:
                raise ValueError("contact_email isn't an email address")
            value = _int(r.get("value_inr"))
            close = _date(r.get("expected_close"))
            owner_email = (_text(r.get("owner_email")) or "").lower()
            if owner_email:
                if owner_email not in owners:
                    raise ValueError("owner_email isn't a member of the sales team")
                if not rights.owner and owners[owner_email] != actor.id:
                    raise ValueError("only an admin owner can import leads for someone else")
                owner_id = owners[owner_email]
            else:
                owner_id = actor.id if actor.id in sales_ids else None
        except ValueError as exc:
            item.update(status="error", error=str(exc))
            results.append(item)
            continue
        website = _text(r.get("website"))
        domain = service.lead_domain(website, [email])
        key = (service.key_of(name), service.key_of(city))
        earlier = seen_names.get(key) or (seen_domains.get(domain) if domain else None)
        if earlier:
            item.update(status="duplicate", error=f"the same as row {earlier}")
            results.append(item)
            continue
        found = await service.duplicates(session, name, city, [domain], limit=1)
        if found:
            item.update(status="duplicate", match_id=found[0].id)
            results.append(item)
            continue
        seen_names[key] = r["row"]
        if domain:
            seen_domains[domain] = r["row"]
        item["status"] = "new"
        if not dry_run:
            contact = {"name": _text(r.get("contact_name")) or email, "role": _text(r.get("contact_role")), "email": email,
                       "phone": _text(r.get("contact_phone")), "whatsapp": _text(r.get("contact_whatsapp"))}
            tags = [t for t in (_text(r.get("tags")) or "").split(",") if t.strip()]
            lead = await service.make_lead(
                session, name=name, type=typ, actor=actor, owner_id=owner_id, city=city, website=website, source=source,
                value_inr=value, expected_close=close, tags=tags, contacts=[contact] if (contact["name"] or email) else [],
                note=_text(r.get("note")) or "Imported from a sheet.",
            )
            item["id"] = lead.id
            created += 1
        results.append(item)
    out = {"rows": results, "dry_run": dry_run,
           "counts": {s: sum(1 for x in results if x["status"] == s) for s in ("new", "duplicate", "error")}}
    if not dry_run:
        out["created"] = created
    return out


def template_bytes() -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    ws = wb.active
    ws.title = "Leads"
    ws.append([c for c, _ in COLUMNS])
    ws.append(EXAMPLE)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    ws.freeze_panes = "A2"
    help_ws = wb.create_sheet("Columns")
    help_ws.append(["Column", "What goes in it"])
    for c, words in COLUMNS:
        help_ws.append([c, words])
    for cell in help_ws[1]:
        cell.font = Font(bold=True)
    help_ws.column_dimensions["A"].width = 20
    help_ws.column_dimensions["B"].width = 80
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()
