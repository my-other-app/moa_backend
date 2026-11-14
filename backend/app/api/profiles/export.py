"""Files: one profile's vCard, and a person's whole network as vCards or a CSV.

What a file holds is what the viewer may see (access.py): the address and
the number only at the owner's share level, the organisation and links only
at theirs, and a private profile's stranger gets the name, headline and
address of the page. vCard 3.0 (RFC 2426): text values escaped, lines folded
at 75 octets, CRLF. The CSV neutralises formulas: a cell starting with
= + - @, a tab or a carriage return is written with a ' in front, so a
spreadsheet shows it as text and never runs it.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from typing import Iterable, Optional

FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")


# ─── vCard ──────────────────────────────────────────────────────────────


def _text(value) -> str:
    """A vCard text value: backslash, comma and semicolon escaped, line breaks as \\n."""
    s = str(value or "")
    s = s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
    return s.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n")


def _uri(value) -> str:
    """A link as a vCard value: no line breaks (links never hold one; this makes sure)."""
    return "".join(ch for ch in str(value or "") if ch not in "\r\n")


def _fold(line: str) -> str:
    """RFC 2426 folding: at most 75 octets a line, a continuation starts with a space."""
    out, current, size = [], "", 0
    for ch in line:
        n = len(ch.encode("utf-8"))
        limit = 75 if not out else 74
        if size + n > limit:
            out.append(current)
            current, size = "", 0
        current += ch
        size += n
    out.append(current)
    return "\r\n ".join(out)


def vcard(*, full_name: str, headline: Optional[str] = None, org: Optional[str] = None, urls: Iterable[str] = (),
          email: Optional[str] = None, phone: Optional[str] = None, note: Optional[str] = None,
          categories: Iterable[str] = ()) -> str:
    first, _, last = (full_name or "").strip().partition(" ")
    lines = [
        "BEGIN:VCARD", "VERSION:3.0",
        f"N:{_text(last)};{_text(first)};;;",
        f"FN:{_text(full_name)}",
    ]
    if headline:
        lines.append(f"TITLE:{_text(headline)}")
    if org:
        lines.append(f"ORG:{_text(org)}")
    if email:
        lines.append(f"EMAIL;TYPE=INTERNET:{_text(email)}")
    if phone:
        lines.append(f"TEL;TYPE=CELL:{_text(phone)}")
    for url in urls:
        if url:
            lines.append(f"URL:{_uri(url)}")
    cats = [c for c in categories if c]
    if cats:
        lines.append("CATEGORIES:" + ",".join(_text(c) for c in cats))
    if note:
        lines.append(f"NOTE:{_text(note)}")
    lines.append("END:VCARD")
    return "".join(_fold(line) + "\r\n" for line in lines)


def profile_vcard(profile: dict, profile_url: str) -> str:
    """One PublicProfile (as this viewer sees it) as a vCard."""
    org = profile.get("org") or {}
    return vcard(
        full_name=profile["full_name"], headline=profile.get("headline"), org=org.get("name"),
        urls=[profile_url, *[link["url"] for link in profile.get("links") or []]],
        email=(profile.get("contact") or {}).get("email"), phone=(profile.get("contact") or {}).get("phone"),
    )


def _when(dt: Optional[datetime]) -> str:
    if dt is None:
        return ""
    from app.core.response.base_model import IST

    return dt.astimezone(IST).strftime("%d %b %Y, %H:%M IST")


def _events(item: dict) -> list[str]:
    names: list[str] = []
    for e in item.get("encounters") or []:
        name = (e.get("event") or {}).get("name")
        if name and name not in names:
            names.append(name)
    return names


def network_vcards(items: list[dict]) -> str:
    out = []
    for item in items:
        person = item["person"]
        note_parts = []
        if item.get("note"):
            note_parts.append(item["note"])
        if item.get("first_met_at"):
            note_parts.append(f"Met {_when(item['first_met_at'])}")
        events = _events(item)
        if events:
            note_parts.append("At " + ", ".join(events))
        out.append(vcard(
            full_name=person["full_name"], headline=person.get("headline"), org=person.get("org_name"),
            urls=[item["profile_url"], *item.get("links", [])], email=item.get("email"), phone=item.get("phone"),
            note="\n".join(note_parts) or None, categories=item.get("tags") or [],
        ))
    return "".join(out)


# ─── CSV ────────────────────────────────────────────────────────────────

CSV_HEADER = ["Name", "Headline", "Organisation", "Profile URL", "Links", "Email", "Phone", "Met at", "Events", "Note", "Tags"]


def csv_cell(value) -> str:
    """Text for a cell, never a formula."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        value = value.astimezone(timezone.utc).isoformat()
    elif isinstance(value, (list, tuple)):
        value = ", ".join("" if v is None else str(v) for v in value)
    text = str(value)
    return "'" + text if text.startswith(FORMULA_STARTS) else text


def network_csv(items: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(CSV_HEADER)
    for item in items:
        person = item["person"]
        writer.writerow([csv_cell(v) for v in (
            person["full_name"], person.get("headline"), person.get("org_name"), item["profile_url"], " ".join(item.get("links", [])),
            item.get("email"), item.get("phone"), item.get("first_met_at"), _events(item), item.get("note"), item.get("tags") or [],
        )])
    return buf.getvalue()
