"""A club's WhatsApp message templates, on its own WhatsApp Business Account.

Outside the 24-hour window after someone last wrote, WhatsApp delivers only a
template Meta approved, so the club needs its own. The dashboard lists them
live from Meta (and keeps what it read in club_whatsapp_templates: a ticket
send decides by it without asking Meta), creates one (Meta reviews it:
PENDING, then APPROVED or REJECTED with a reason, which Meta's webhook
`message_template_status_update` tells us) and deletes one.

What is submitted is checked here first, as Meta would refuse it: the name,
the language, a body with numbered variables {{1}}..{{n}} in order, one
example for each, never a variable first or last, the header, the footer and
the buttons within Meta's limits. A refusal names the field.

`STANDARD` are MyOtherApp's own ticket, reminder and certificate templates,
with the values our sends fill in the same order (app/core/whatsapp.py); their
names are kept for them (`build` refuses them). A club that submits them
(`install_standard`) has its tickets, certificates and reminders sent from its
own number once Meta approves them -- and only while the approved template is
the shape our sends fill (`fits`); until then they go from the platform's.

What is kept is updated in place: our own UNUSABLE mark (Meta refused a send
of it from the club's number) lasts until Meta says something new of the
template, and Meta's FLAGGED or LOCKED is kept beside its status. The sweep
reads every active number's templates again (sweep.refresh_templates).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import delete, select

from app.api.club_whatsapp import graph
from app.api.club_whatsapp.models import ClubWhatsAppAccounts, ClubWhatsAppTemplates
from app.config import settings
from app.response import CustomHTTPException

NAME = re.compile(r"^[a-z0-9_]{1,512}$")
LANGUAGE = re.compile(r"^[a-z]{2,3}(?:_[A-Z]{2})?$")
VARIABLE = re.compile(r"\{\{\s*(\d+)\s*\}\}")
ANY_BRACES = re.compile(r"\{\{(.*?)\}\}")
CATEGORIES = ("UTILITY", "MARKETING")
BUTTON_TYPES = ("QUICK_REPLY", "URL", "PHONE_NUMBER")

BODY_MAX = 1024
HEADER_MAX = 60
FOOTER_MAX = 60
BUTTON_TEXT_MAX = 25
BUTTONS_MAX = 10


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _bad(field: str, message: str) -> CustomHTTPException:
    return CustomHTTPException(status_code=422, message=message, error_code="INVALID_TEMPLATE", errors={"field": field})


# ─── Reading what Meta has ───────────────────────────────────────────────

APPROVED = "APPROVED"
PENDING = "PENDING"
#: Our own mark: Meta refused a send of it from the club's number (a
#: template code), so the club's tickets go from the platform's until Meta
#: reports a new status for it.
UNUSABLE = "UNUSABLE"
#: The statuses Meta gives a template (its template API's enum). An event
#: outside these, and UNARCHIVED, is answered by reading the template again.
META_STATUSES = frozenset({"APPROVED", "ARCHIVED", "DELETED", "DISABLED", "IN_APPEAL", "LIMIT_EXCEEDED", "PAUSED",
                           "PENDING", "PENDING_DELETION", "REJECTED"})
#: Events that say something of a template without changing its status: kept beside it.
FLAGS = frozenset({"FLAGGED", "LOCKED"})
NAMED = re.compile(r"\{\{\s*([a-z_][a-z0-9_]*)\s*\}\}")
POSITIONAL, NAMED_FORMAT = "POSITIONAL", "NAMED"


def language_of(raw) -> str:
    """Meta's webhooks say "en-US", its template API "en_US": one spelling."""
    return str(raw or "").strip().replace("-", "_")[:16]


def _component(components: list, kind: str) -> Optional[dict]:
    for c in components or []:
        if isinstance(c, dict) and str(c.get("type") or "").upper() == kind:
            return c
    return None


def variables(text: Optional[str]) -> list[int]:
    return [int(n) for n in VARIABLE.findall(text or "")]


def _names(text: Optional[str]) -> list[str]:
    """A text's named parameters ({{first_name}}), each once, in order."""
    out: list[str] = []
    for name in NAMED.findall(text or ""):
        if name not in out:
            out.append(name)
    return out


def shape(components: Optional[list]) -> dict:
    """What a send must fill: the header's variables (and its format), the
    body's, and each URL button's. A template made with named parameters
    ({{first_name}}) is counted by its names, kept in order (`body_names`),
    and sent with them (`parameter_format` NAMED)."""
    header = _component(components or [], "HEADER")
    body = _component(components or [], "BODY")
    footer = _component(components or [], "FOOTER")
    buttons = (_component(components or [], "BUTTONS") or {}).get("buttons") or []
    out_buttons = []
    for i, b in enumerate(buttons):
        if not isinstance(b, dict):
            continue
        kind = str(b.get("type") or "").upper()
        out_buttons.append({
            "index": i, "type": kind, "text": b.get("text"), "url": b.get("url"), "phone_number": b.get("phone_number"),
            "variables": len(set(variables(b.get("url")))) if kind == "URL" else 0,
        })
    header_format = str((header or {}).get("format") or "").upper() or None
    header_text = (header or {}).get("text") if header_format == "TEXT" else None
    body_text = (body or {}).get("text") or ""
    header_names, body_names = _names(header_text), _names(body_text)
    named = bool(header_names or body_names)
    return {
        "header": {"format": header_format, "text": (header or {}).get("text"),
                   "variables": (len(header_names) if named else len(set(variables(header_text)))) if header_format == "TEXT" else 0,
                   "names": header_names} if header else None,
        "body": body_text,
        "body_variables": len(body_names) if named else len(set(variables(body_text))),
        "body_names": body_names,
        "parameter_format": NAMED_FORMAT if named else POSITIONAL,
        "footer": (footer or {}).get("text"),
        "buttons": out_buttons,
    }


def fits(name: str, components: Optional[list], category: Optional[str]) -> bool:
    """Whether the club's template of one of MyOtherApp's own names
    (STANDARD) is the shape our sends fill (app/core/whatsapp.py): a
    UTILITY template with components; a DOCUMENT header exactly when ours
    carries a document (else no header that needs a value); the same
    number of numbered body variables; and one URL button with one
    variable, the first, as ours has -- no other button that needs a value.
    A template of that name in another shape would be refused at every send
    (132000), so it is not routed to."""
    spec = STANDARD.get(name)
    if spec is None or not components or str(category or "").upper() != "UTILITY":
        return False
    d = shape(components)
    if d["parameter_format"] != POSITIONAL:
        return False
    header = d["header"]
    if spec["document"]:
        if header is None or header["format"] != "DOCUMENT":
            return False
    elif header is not None and (header["format"] != "TEXT" or header["variables"]):
        return False
    if d["body_variables"] != len(spec["examples"]):
        return False
    if any(b["type"] not in BUTTON_TYPES for b in d["buttons"]):
        return False
    return [(b["index"], b["variables"]) for b in d["buttons"] if b["variables"]] == [(0, 1)] and d["buttons"][0]["type"] == "URL"


def template_out(row: dict) -> dict:
    """A template as the dashboard shows it, from Meta's answer or the cache."""
    components = row.get("components") or []
    flag = row.get("flag")
    return {
        "id": row.get("id") or row.get("meta_id"),
        "name": row.get("name"),
        "language": row.get("language"),
        "category": row.get("category"),
        "status": row.get("status"),
        "rejected_reason": None if row.get("rejected_reason") in (None, "", "NONE") else row.get("rejected_reason"),
        "quality": row.get("quality"),
        "flag": flag,
        "flagged": flag == "FLAGGED",
        "components": components,
        **shape(components),
    }


def _quality(row: dict) -> Optional[str]:
    score = row.get("quality_score")
    if isinstance(score, dict):
        return (score.get("score") or None) and str(score.get("score"))[:16]
    return None


def _keep(account: ClubWhatsAppAccounts, row: Optional[ClubWhatsAppTemplates], t: dict, now: datetime) -> ClubWhatsAppTemplates:
    """Meta's answer for one template onto its row. An UNUSABLE mark stays
    while Meta says what it said when the mark was made; a FLAGGED or
    LOCKED one while its status is unchanged."""
    status = str(t.get("status") or "UNKNOWN")[:32]
    if row is None:
        row = ClubWhatsAppTemplates(account_id=account.id, club_id=account.club_id, name=str(t.get("name"))[:512],
                                    language=language_of(t.get("language")))
    changed = row.meta_status != status
    row.status = UNUSABLE if (row.status == UNUSABLE and not changed) else status
    row.meta_status = status
    if changed:
        row.flag = None
    row.meta_id = str(t.get("id") or "")[:64] or row.meta_id
    row.category = (t.get("category") or None) and str(t.get("category"))[:32]
    row.rejected_reason = (t.get("rejected_reason") or None) and str(t.get("rejected_reason"))[:200]
    row.quality = _quality(t)
    row.components = t.get("components") or []
    row.synced_at = now
    return row


async def sync(session, account: ClubWhatsAppAccounts, keys: graph.Keys) -> list[dict]:
    """Every template on the club's account, read from Meta and kept (each
    row updated in place, so our own marks last as long as they should; one
    Meta no longer has is dropped). Meta is asked before anything is
    written: a refusal leaves the cache as it was. The templates as kept."""
    found = await graph.list_templates(keys)
    now = _now()
    rows = {(r.name, r.language): r for r in (await session.scalars(
        select(ClubWhatsAppTemplates).where(ClubWhatsAppTemplates.account_id == account.id)
    )).all()}
    seen = set()
    for t in found:
        key = (str(t.get("name") or "")[:512], language_of(t.get("language")))
        if not key[0] or not key[1] or key in seen:
            continue
        seen.add(key)
        session.add(_keep(account, rows.get(key), t, now))
    for key, row in rows.items():
        if key not in seen:
            await session.delete(row)
    account.templates_synced_at = now
    await session.flush()
    return await cached(session, account.id)


async def cached(session, account_id: int) -> list[dict]:
    rows = (await session.scalars(
        select(ClubWhatsAppTemplates).where(ClubWhatsAppTemplates.account_id == account_id)
        .order_by(ClubWhatsAppTemplates.name, ClubWhatsAppTemplates.language)
    )).all()
    return [template_out({
        "meta_id": r.meta_id, "name": r.name, "language": r.language, "category": r.category, "status": r.status,
        "rejected_reason": r.rejected_reason, "quality": r.quality, "components": r.components or [], "flag": r.flag,
    }) for r in rows]


def _preference(language: str) -> tuple:
    """The platform's language first (WHATSAPP_TEMPLATE_LANGUAGE), then
    English, then the rest alphabetically: the same answer every time."""
    wanted = language_of(settings.WHATSAPP_TEMPLATE_LANGUAGE or "en")
    order = [wanted, "en", "en_US", "en_GB"]
    return (order.index(language) if language in order else len(order), language)


async def approved(session, account_id: int, name: str, language: Optional[str]) -> Optional[ClubWhatsAppTemplates]:
    """The approved template of this name: in `language`, or -- none given --
    in the preferred language it is approved in (_preference)."""
    q = select(ClubWhatsAppTemplates).where(
        ClubWhatsAppTemplates.account_id == account_id, ClubWhatsAppTemplates.name == name,
        ClubWhatsAppTemplates.status == APPROVED,
    )
    if language:
        q = q.where(ClubWhatsAppTemplates.language == language_of(language))
    rows = (await session.scalars(q)).all()
    return min(rows, key=lambda r: _preference(r.language)) if rows else None


async def usable_language(session, account_id: int, name: str) -> Optional[str]:
    """The language the club's account has one of MyOtherApp's templates
    approved in, in the shape our sends fill (`fits`), the preferred first;
    None when it has none such."""
    rows = (await session.scalars(
        select(ClubWhatsAppTemplates).where(
            ClubWhatsAppTemplates.account_id == account_id, ClubWhatsAppTemplates.name == name,
            ClubWhatsAppTemplates.status == APPROVED,
        )
    )).all()
    good = [r.language for r in rows if fits(name, r.components, r.category)]
    return min(good, key=_preference) if good else None


def standard_state(rows: list) -> dict[str, Optional[ClubWhatsAppTemplates]]:
    """For each of MyOtherApp's names, the club's template that counts as
    having it -- APPROVED or PENDING, in the shape our sends fill (a pending
    one Meta hasn't shown us yet counts) -- or None."""
    out: dict[str, Optional[ClubWhatsAppTemplates]] = {}
    for name in STANDARD:
        mine = [r for r in rows if r.name == name and r.status in (APPROVED, PENDING)
                and (fits(name, r.components, r.category) or (r.status == PENDING and not r.components))]
        out[name] = min(mine, key=lambda r: (r.status != APPROVED, _preference(r.language))) if mine else None
    return out


async def standard_missing(session, account_id: int) -> list[str]:
    rows = (await session.scalars(select(ClubWhatsAppTemplates).where(ClubWhatsAppTemplates.account_id == account_id))).all()
    return [name for name, row in standard_state(list(rows)).items() if row is None]


async def reread(session, account: ClubWhatsAppAccounts, name: str, language: str) -> bool:
    """One template read from Meta again (its status, components), for an
    event that says something changed without saying what: UNARCHIVED, an
    unknown event, one of ours approved. Best effort -- the sweep's sync
    comes round anyway -- and never for a number the plan no longer covers
    or whose keys can't be read. True when Meta answered."""
    from app.api.club_whatsapp import crypto
    from app.api.club_whatsapp import service as accounts

    if not await accounts.plan_allows(session, account.club_id):
        return False
    try:
        keys = accounts.keys_of(account)
        found = await graph.list_templates(keys, name=name)
    except (graph.GraphError, crypto.Unavailable):
        return False
    row = await session.scalar(select(ClubWhatsAppTemplates).where(
        ClubWhatsAppTemplates.account_id == account.id, ClubWhatsAppTemplates.name == name, ClubWhatsAppTemplates.language == language,
    ))
    for t in found:
        if str(t.get("name") or "") == name and language_of(t.get("language")) == language:
            session.add(_keep(account, row, t, _now()))
            return True
    if row is not None:
        # Meta has no such template any more.
        await session.delete(row)
    return True


async def status_update(session, account: ClubWhatsAppAccounts, value: dict) -> None:
    """Meta's `message_template_status_update`. A status (APPROVED, REJECTED
    with its reason, PAUSED, DISABLED, ARCHIVED ...) replaces what was kept
    -- our own UNUSABLE mark too; REINSTATED is APPROVED again; FLAGGED and
    LOCKED keep the status and are kept beside it (`flag`); a deleted one
    is dropped. UNARCHIVED (restored to its earlier status) or an event
    outside Meta's statuses is read from Meta again, and so is one of
    MyOtherApp's own templates once approved (its components, for routing)."""
    name = str(value.get("message_template_name") or "")
    language = language_of(value.get("message_template_language"))
    event = str(value.get("event") or "").upper()
    if not name or not event:
        return
    q = select(ClubWhatsAppTemplates).where(ClubWhatsAppTemplates.account_id == account.id, ClubWhatsAppTemplates.name == name)
    if language:
        q = q.where(ClubWhatsAppTemplates.language == language)
    rows = (await session.scalars(q)).all()
    reason = value.get("reason")
    reason = None if reason in (None, "", "NONE") else str(reason)[:200]
    if event == "REINSTATED":
        event = APPROVED
    if event in FLAGS:
        for row in rows:
            row.flag = event
        return
    if event in ("DELETED", "PENDING_DELETION"):
        for row in rows:
            await session.delete(row)
        return
    if event not in META_STATUSES:
        if language:
            await reread(session, account, name, language)
        return
    if not rows and language:
        rows = [ClubWhatsAppTemplates(
            account_id=account.id, club_id=account.club_id, meta_id=str(value.get("message_template_id") or "")[:64] or None,
            name=name[:512], language=language, components=[],
        )]
        session.add(rows[0])
    for row in rows:
        row.status = row.meta_status = event[:32]
        row.rejected_reason = reason
        row.flag = None
        if value.get("message_template_category"):
            row.category = str(value["message_template_category"]).upper()[:32]
        row.synced_at = _now()
    if event == APPROVED and name in STANDARD and language:
        await session.flush()
        await reread(session, account, name, language)


async def category_update(session, account: ClubWhatsAppAccounts, value: dict) -> None:
    """Meta's `template_category_update`: once a template's category has
    changed (`previous_category` given), it is kept -- a ticket template
    moved to MARKETING is no longer one our sends use. A change only
    announced for later (`correct_category`) waits for that."""
    if not value.get("previous_category") or not value.get("new_category"):
        return
    rows = (await session.scalars(select(ClubWhatsAppTemplates).where(
        ClubWhatsAppTemplates.account_id == account.id,
        ClubWhatsAppTemplates.name == str(value.get("message_template_name") or ""),
        ClubWhatsAppTemplates.language == language_of(value.get("message_template_language")),
    ))).all()
    for row in rows:
        row.category = str(value["new_category"]).upper()[:32]


# ─── Checking what is submitted ──────────────────────────────────────────


def _check_variables(field: str, text: str, examples: list, *, at_ends: bool = True, most: Optional[int] = None) -> list[str]:
    for inner in ANY_BRACES.findall(text):
        if not inner.strip().isdigit():
            raise _bad(field, "Variables are numbered: {{1}}, {{2}}, and so on.")
    found = variables(text)
    numbers = sorted(set(found))
    if numbers and numbers != list(range(1, len(numbers) + 1)):
        raise _bad(field, "Number the variables in order from {{1}}, with none missing.")
    if most is not None and len(numbers) > most:
        raise _bad(field, f"This can have at most {most} variable{'s' if most != 1 else ''}.")
    stripped = text.strip()
    if at_ends and numbers and (VARIABLE.match(stripped) or re.search(r"\{\{\s*\d+\s*\}\}$", stripped)):
        raise _bad(field, "WhatsApp doesn't accept a variable at the very start or end. Put some words before and after it.")
    cleaned = [" ".join(str(e).split()) for e in (examples or [])]
    if len(cleaned) != len(numbers) or any(not e for e in cleaned):
        raise _bad(f"{field}_examples", f"Give an example for each variable ({len(numbers)}), so Meta can review it.")
    return cleaned


def build(payload: dict) -> dict:
    """The body of POST /{waba}/message_templates, from the dashboard's form,
    checked as Meta would check it."""
    name = str(payload.get("name") or "").strip()
    if not NAME.match(name):
        raise _bad("name", "Use lowercase letters, numbers and underscores only, like event_update.")
    if name in STANDARD:
        raise _bad("name", f"{name} is MyOtherApp's own template, which your tickets go by. Add it with "
                           "\"Submit MyOtherApp's templates\", or give yours another name.")
    language = language_of(payload.get("language"))
    if not LANGUAGE.match(language):
        raise _bad("language", "Pick a language, like en or en_US.")
    category = str(payload.get("category") or "").strip().upper()
    if category not in CATEGORIES:
        raise _bad("category", "Pick Utility (about something they signed up for) or Marketing.")
    components: list[dict] = []

    header = payload.get("header") or None
    if header and (header.get("text") or "").strip():
        text = str(header["text"]).strip()
        if len(text) > HEADER_MAX:
            raise _bad("header", f"The header is at most {HEADER_MAX} characters.")
        examples = _check_variables("header", text, header.get("examples") or [], at_ends=False, most=1)
        component = {"type": "HEADER", "format": "TEXT", "text": text}
        if examples:
            component["example"] = {"header_text": examples}
        components.append(component)

    body = payload.get("body") or {}
    text = str(body.get("text") or "").strip()
    if not text:
        raise _bad("body", "Write the message.")
    if len(text) > BODY_MAX:
        raise _bad("body", f"The message is at most {BODY_MAX} characters.")
    examples = _check_variables("body", text, body.get("examples") or [])
    component = {"type": "BODY", "text": text}
    if examples:
        component["example"] = {"body_text": [examples]}
    components.append(component)

    footer = str(payload.get("footer") or "").strip()
    if footer:
        if len(footer) > FOOTER_MAX:
            raise _bad("footer", f"The footer is at most {FOOTER_MAX} characters.")
        if ANY_BRACES.search(footer):
            raise _bad("footer", "The footer can't have variables.")
        components.append({"type": "FOOTER", "text": footer})

    buttons = payload.get("buttons") or []
    if len(buttons) > BUTTONS_MAX:
        raise _bad("buttons", f"At most {BUTTONS_MAX} buttons.")
    out = []
    urls = phones = 0
    for i, b in enumerate(buttons):
        kind = str((b or {}).get("type") or "").upper()
        label = str((b or {}).get("text") or "").strip()
        field = f"buttons.{i}"
        if kind not in BUTTON_TYPES:
            raise _bad(field, "A button is a quick reply, a link or a phone number.")
        if not label or len(label) > BUTTON_TEXT_MAX:
            raise _bad(field, f"A button's text is 1 to {BUTTON_TEXT_MAX} characters.")
        if kind == "QUICK_REPLY":
            out.append({"type": "QUICK_REPLY", "text": label})
        elif kind == "URL":
            urls += 1
            link = str(b.get("url") or "").strip()
            if not link.startswith("https://") or len(link) > 2000:
                raise _bad(field, "A link button needs an address starting https://.")
            found = variables(link)
            if found and (found != [1] or not re.search(r"\{\{\s*1\s*\}\}$", link)):
                raise _bad(field, "A link can have one variable, {{1}}, at its very end.")
            button = {"type": "URL", "text": label, "url": link}
            if found:
                example = str(b.get("example") or "").strip()
                if not example.startswith("https://"):
                    raise _bad(field, "Give an example of the whole address, starting https://.")
                button["example"] = [example]
            out.append(button)
        else:
            phones += 1
            number = re.sub(r"[\s\-()]", "", str(b.get("phone_number") or ""))
            if not re.fullmatch(r"\+?\d{8,20}", number):
                raise _bad(field, "A phone button needs the number with its country code.")
            out.append({"type": "PHONE_NUMBER", "text": label, "phone_number": number.lstrip("+")})
    if urls > 2 or phones > 1:
        raise _bad("buttons", "At most two link buttons and one phone button.")
    if out:
        components.append({"type": "BUTTONS", "buttons": out})
    return {"name": name, "language": language, "category": category, "components": components}


# ─── A send's components ─────────────────────────────────────────────────


def send_components(definition: dict, params: dict) -> tuple[list[dict], dict]:
    """The `components` of a template send, from the values the dashboard
    gives (`header`, `body`, `buttons` {index: value}), checked against the
    template's own variables (by name, in their order, for a template made
    with named parameters). Also what is kept on the message."""
    header_values = [str(v) for v in (params.get("header") or [])]
    body_values = [str(v) for v in (params.get("body") or [])]
    button_values = {str(k): str(v) for k, v in (params.get("buttons") or {}).items()}

    def tidy(field: str, values: list[str], n: int) -> list[str]:
        cleaned = [" ".join(v.split()) for v in values]
        if len(cleaned) != n or any(not v for v in cleaned):
            raise _bad(field, f"This template needs {n} value{'s' if n != 1 else ''} there.")
        if any(len(v) > 1024 for v in cleaned):
            raise _bad(field, "A value is too long.")
        return cleaned

    def parameters(values: list[str], names: list[str]) -> list[dict]:
        if definition.get("parameter_format") == NAMED_FORMAT:
            return [{"type": "text", "parameter_name": n, "text": v} for n, v in zip(names, values)]
        return [{"type": "text", "text": v} for v in values]

    components: list[dict] = []
    header = definition.get("header")
    kept: dict[str, Any] = {}
    if header and header.get("format") == "TEXT" and header.get("variables"):
        kept["header"] = tidy("header", header_values, header["variables"])
        components.append({"type": "header", "parameters": parameters(kept["header"], header.get("names") or [])})
    elif header and header.get("format") in ("DOCUMENT", "IMAGE", "VIDEO", "LOCATION"):
        raise _bad("header", "Templates with a picture, video or document header can't be sent from the inbox yet.")
    n = int(definition.get("body_variables") or 0)
    kept["body"] = tidy("body", body_values, n)
    if n:
        components.append({"type": "body", "parameters": parameters(kept["body"], definition.get("body_names") or [])})
    kept["buttons"] = {}
    for b in definition.get("buttons") or []:
        if b.get("type") == "URL" and b.get("variables"):
            value = tidy(f"buttons.{b['index']}", [button_values.get(str(b["index"]), "")], 1)[0]
            kept["buttons"][str(b["index"])] = value
            components.append({"type": "button", "sub_type": "url", "index": str(b["index"]),
                               "parameters": [{"type": "text", "text": value}]})
    return components, kept


def render(definition: dict, kept: dict) -> str:
    """The template's text with the values in, as the person saw it."""
    def fill(text: Optional[str], values: list, names: Optional[list] = None) -> str:
        if names:
            by_name = dict(zip(names, values))
            return NAMED.sub(lambda m: by_name.get(m.group(1), m.group(0)), text or "")
        return VARIABLE.sub(lambda m: values[int(m.group(1)) - 1] if 0 < int(m.group(1)) <= len(values) else m.group(0), text or "")

    parts = []
    header = definition.get("header") or {}
    if header.get("format") == "TEXT" and header.get("text"):
        parts.append(fill(header["text"], kept.get("header") or [], header.get("names")))
    parts.append(fill(definition.get("body"), kept.get("body") or [], definition.get("body_names")))
    if definition.get("footer"):
        parts.append(definition["footer"])
    return "\n\n".join(p for p in parts if p)


# ─── MyOtherApp's own: tickets, reminders, certificates ──────────────────

TICKET_URL = "https://myotherapp.in/tickets/{{1}}"
VERIFY_URL = "https://myotherapp.in/verify/{{1}}"

STANDARD: dict[str, dict] = {
    "ticket_confirmed": {
        "document": True,
        "body": "Hi {{1}}, you're registered for {{2}} on {{3}}. Your ticket ({{4}}) is attached. Show its QR code at the entry.",
        "examples": ["Ananya", "Build Night", "Fri 20 Nov 2026, 6:30 PM", "MOA-ABCDE-12345"],
        "button": ("View ticket", TICKET_URL, "https://myotherapp.in/tickets/MOA-ABCDE-12345"),
    },
    "event_reminder": {
        "document": False,
        "body": "Hi {{1}}, a reminder that {{2}} starts {{3}} at {{4}}. Keep your ticket ready at the entry.",
        "examples": ["Ananya", "Build Night", "Fri 20 Nov 2026, 6:30 PM", "Innovation Hall"],
        "button": ("View ticket", TICKET_URL, "https://myotherapp.in/tickets/MOA-ABCDE-12345"),
    },
    "certificate_ready": {
        "document": True,
        "body": "Hi {{1}}, thank you for attending {{2}}. Your certificate from {{3}} is attached. "
                "Anyone can check it is genuine on myotherapp.in with the number {{4}}.",
        "examples": ["Ananya", "Build Night", "Yukthi", "CERT-2026-0001"],
        "button": ("Verify certificate", VERIFY_URL, "https://myotherapp.in/verify/CERT-2026-0001"),
    },
}

#: A one-page PDF: the sample Meta reviews a document header with.
SAMPLE_PDF = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 100]>>endobj\n"
    b"xref\n0 4\n0000000000 65535 f \n0000000009 00000 n \n0000000052 00000 n \n0000000101 00000 n \n"
    b"trailer<</Size 4/Root 1 0 R>>\nstartxref\n164\n%%EOF\n"
)


def standard_body(name: str, handle: Optional[str]) -> dict:
    spec = STANDARD[name]
    components: list[dict] = []
    if spec["document"]:
        components.append({"type": "HEADER", "format": "DOCUMENT", "example": {"header_handle": [handle]}})
    components.append({"type": "BODY", "text": spec["body"], "example": {"body_text": [spec["examples"]]}})
    label, link, example = spec["button"]
    components.append({"type": "BUTTONS", "buttons": [{"type": "URL", "text": label, "url": link, "example": [example]}]})
    return {"name": name, "language": language_of(settings.WHATSAPP_TEMPLATE_LANGUAGE or "en"), "category": "UTILITY",
            "components": components}


#: What a template of one of MyOtherApp's names that can't be resubmitted says.
_NOT_OURS = ("Your account has {name} ({status}), but not in the shape MyOtherApp's {kind} fills, so they go from "
             "MyOtherApp's number. Delete it in WhatsApp Manager and submit again here -- Meta won't take a deleted "
             "approved template's name again for 30 days.")
_KIND = {"ticket_confirmed": "tickets", "event_reminder": "reminders", "certificate_ready": "certificates"}


async def install_standard(session, account: ClubWhatsAppAccounts, keys: graph.Keys) -> dict[str, str]:
    """Submit each of MyOtherApp's templates the club's account does not
    have in a shape our sends fill (as last read: the caller syncs first).
    {name: "exists" | "pending" | "submitted" | why not}. One it has but
    Meta rejected (or never approved) is deleted and submitted again; one
    approved, paused or disabled in another shape is left, with what to do
    (a deleted approved name can't be used again for 30 days)."""
    out: dict[str, str] = {}
    rows = list((await session.scalars(select(ClubWhatsAppTemplates).where(ClubWhatsAppTemplates.account_id == account.id))).all())
    have = standard_state(rows)
    ours = language_of(settings.WHATSAPP_TEMPLATE_LANGUAGE or "en")
    handle: Optional[str] = None
    for name, spec in STANDARD.items():
        if have[name] is not None:
            out[name] = "exists" if have[name].status == APPROVED else "pending"
            continue
        same = next((r for r in rows if r.name == name and r.language == ours), None)
        try:
            if same is not None:
                if same.status not in ("REJECTED", PENDING, "IN_APPEAL", "LIMIT_EXCEEDED"):
                    out[name] = _NOT_OURS.format(name=name, status=same.status, kind=_KIND[name])
                    continue
                # Never approved: its name is free again once it is gone.
                await graph.delete_template(keys, name, same.meta_id)
            if spec["document"] and handle is None:
                handle = await graph.upload_sample(keys, SAMPLE_PDF, "sample.pdf", "application/pdf")
            await graph.create_template(keys, standard_body(name, handle))
            out[name] = "submitted"
        except graph.GraphError as exc:
            if exc.code == "APP_ID":
                out[name] = "Add your app ID in the connection settings first: a document header needs it."
            else:
                out[name] = graph.explain_template(exc)[2]
    return out
