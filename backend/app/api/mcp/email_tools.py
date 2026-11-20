"""An event's emails and the club's email library, for an assistant (scopes
emails:read, emails:write).

The emails are the dashboard's (app/api/emails): an event's four journey
emails (the invite, which is the ticket email; the day-before note; the
reminder as the doors open; the thank-you), its custom emails, and the six
registration review emails of an event that reviews registrations (the
club's words unless the event has its own copy). Each is named by a key:
its stage in lower case (`invite`, `request_declined`) or `custom:<id>`.

What an assistant may change is the dashboard's save, through the same
service functions and their checks (tokens, block shapes, pictures that are
the club's own): an email's subject, its blocks and theme, its name, and
switching it off. Switching one on, its timer, its audience and what it
attaches stay in the dashboard: each of those decides that guests are sent
something. A preview is drawn for the sample guest, never a real one; a test
goes only to the address of the person who connected the assistant, a few
an hour (APP_MCP_TEST_EMAIL_LIMIT). Sending a review email again to the
people it went to is a request a person approves (actions.py).
"""
from __future__ import annotations

import logging
import math
import time
import uuid
from functools import lru_cache
from typing import Optional

from limits import parse_many
from pydantic import ValidationError
from sqlalchemy.orm import joinedload

from app.api.events.models import Events
from app.api.mcp.oauth import dashboard_url
from app.api.mcp.tools import EVENT_ID, Call, ToolError, _event, _iso, _record, from_http, obj, tool
from app.config import settings
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

KEY = {
    "type": "string", "minLength": 3, "maxLength": 60,
    "description": "Which email: invite, pre_event, reminder, post_event, custom:<id>, or a registration review email "
                   "(request_received, request_approved, request_approved_pay, request_declined, request_lapsed, "
                   "request_unanswered). list_event_emails / list_library_emails give them.",
}
BLOCKS = {
    "type": "array", "maxItems": 60, "items": {"type": "object"},
    "description": (
        "The email's blocks, top to bottom (replaces all of them). Each {id, kind, ...}: heading {text, level 1|2}; "
        "paragraph {text} (**bold**, _italic_, [label](url) inside); list {items: [...], ordered}; button {label, href, "
        "variant primary|secondary}; image {source upload|org_logo|event_icon|qr, imageKey (an upload already in this "
        "email), alt, width 10-100 (%), href}; divider {color}; spacer {height}; details {rows: [{label, value}]}; "
        "ticket_card {showQr, showVenue, eyebrow, title, rows, note}. Any block may have style {align, color, "
        "background, size, weight \"400\"|\"600\"|\"700\", paddingTop, paddingBottom, letterSpacing}. Text may use "
        "{{tokens}} (get_event_email lists them) and {% if token %}...{% endif %}."
    ),
}
THEME = {
    "type": "object",
    "description": "Changes to the look (only the fields given change): font sans|serif|mono, background, surface, text, "
                   "muted, accent (#RRGGBB or brand), accentText, radius 0-32, logo org_logo|none, footer (text with tokens).",
}
EDITS = {
    "subject": {"type": "string", "minLength": 1, "maxLength": 200, "description": "The subject line; tokens allowed."},
    "blocks": BLOCKS,
    "theme": THEME,
}


# ─── Which email ─────────────────────────────────────────────────────────


def _stages():
    from app.core.mail.schema import JOURNEY_STAGES, REQUEST_STAGES

    return JOURNEY_STAGES, REQUEST_STAGES


def key_of(tpl) -> str:
    from app.api.emails.models import EmailStage

    return f"custom:{tpl.id}" if tpl.stage is EmailStage.CUSTOM else tpl.stage.value.lower()


def refused(e: CustomHTTPException) -> ToolError:
    """The dashboard's refusal, in the tools' words: its checks of an email (a
    422 with no code of its own) are INVALID_EMAIL."""
    if e.status_code == 422 and not e.error_code:
        errors = e.errors if isinstance(e.errors, dict) else {}
        problems = (errors.get("fields") or {}).get("design") if isinstance(errors.get("fields"), dict) else None
        return ToolError(e.message or "This email has a problem.", "INVALID_EMAIL", {"problems": problems or [e.message]})
    return from_http(e)


def _not_found(key: str) -> ToolError:
    return ToolError(f"There is no email {key!r} here. list_event_emails / list_library_emails give the keys.", "EMAIL_NOT_FOUND")


def _custom_id(key: str, raw: str):
    try:
        return uuid.UUID(key.split(":", 1)[1])
    except (ValueError, IndexError):
        raise _not_found(raw)


async def _event_email(call: Call, event: Events, raw: str) -> dict:
    """The event's email behind a key: the template, what it is, whose words."""
    from app.api.emails import review, service
    from app.api.emails.models import EmailStage

    journey, requests = _stages()
    key = (raw or "").strip().lower()
    if key.startswith("custom:"):
        try:
            tpl = await service.template_of(call.session, _custom_id(key, raw), event=event)
        except CustomHTTPException:
            raise _not_found(raw)
        if tpl.stage is not EmailStage.CUSTOM:
            raise _not_found(raw)
        return {"tpl": tpl, "group": "custom", "stage": tpl.stage, "source": "event"}
    if key.upper() in journey:
        stage = EmailStage(key.upper())
        tpl = next((t for t in await service.ensure_event_templates(call.session, event) if t.stage is stage), None)
        if tpl is None:
            raise _not_found(raw)
        return {"tpl": tpl, "group": "journey", "stage": stage, "source": "event"}
    if key.upper() in requests:
        stage = EmailStage(key.upper())
        own = await review.event_copy(call.session, event, stage)
        source, tpl = review._source(stage, {stage: own} if own is not None else {}, await review._club_side(call.session, event))
        return {"tpl": tpl, "group": "review", "stage": stage, "source": source}
    raise _not_found(raw)


async def _library_email(call: Call, raw: str) -> dict:
    from app.api.emails import service
    from app.api.emails.models import EmailStage

    journey, requests = _stages()
    key = (raw or "").strip().lower()
    library = await service.ensure_library(call.session, call.club.id)
    if key.startswith("custom:"):
        wanted = _custom_id(key, raw)
        tpl = next((t for t in library if t.id == wanted and t.stage is EmailStage.CUSTOM), None)
    elif key.upper() in (*journey, *requests):
        tpl = next((t for t in library if t.stage.value == key.upper()), None)
    else:
        tpl = None
    if tpl is None:
        raise _not_found(raw)
    group = "custom" if tpl.stage is EmailStage.CUSTOM else ("review" if key.upper() in requests else "journey")
    return {"tpl": tpl, "group": group, "stage": tpl.stage, "source": "library"}


# ─── In words ────────────────────────────────────────────────────────────


def _span(minutes: int) -> str:
    if minutes % 1440 == 0:
        n, unit = minutes // 1440, "day"
    elif minutes % 60 == 0:
        n, unit = minutes // 60, "hour"
    else:
        n, unit = minutes, "minute"
    return f"{n} {unit}{'' if n == 1 else 's'}"


def sends(fields, stage) -> str:
    """When the email goes, as the dashboard says it."""
    from app.core.mail.schema import EMAIL_STAGE_META, REQUEST_STAGES

    if stage.value in REQUEST_STAGES:
        return f"by itself, {EMAIL_STAGE_META[stage.value].get('when')}"
    t = fields.trigger
    if t.mode == "ON_TICKETS":
        return "with each guest's ticket, as tickets go out"
    if t.mode == "BEFORE_START":
        return f"by itself, {_span(t.minutes or 0)} before the event starts"
    if t.mode == "AFTER_END":
        return f"by itself, {_span(t.minutes or 0)} after the event ends"
    return "only when someone sends it from the dashboard"


def _audience(fields) -> str:
    who = {"ALL": "everyone registered", "CHECKED_IN": "those who checked in", "NOT_CHECKED_IN": "those who did not check in"}
    out = who.get(fields.audience.who, fields.audience.who)
    return out + (f" ({', '.join(fields.audience.roles)})" if fields.audience.roles else "")


@lru_cache(maxsize=1)
def _style_defaults() -> dict:
    from app.core.mail.schema import BlockStyle

    return BlockStyle().model_dump(by_alias=True)


def design_out(design) -> dict:
    """The design as the editor saves it, without the block styles left at their defaults."""
    out = design.model_dump(by_alias=True)
    defaults = _style_defaults()
    for b in out.get("blocks") or []:
        style = {k: v for k, v in (b.get("style") or {}).items() if defaults.get(k) != v}
        if style:
            b["style"] = style
        else:
            b.pop("style", None)
    return out


def email_out(found: dict, *, event: Optional[Events] = None) -> dict:
    from app.api.emails import service
    from app.core.mail.schema import EMAIL_STAGE_META

    tpl, stage, group = found["tpl"], found["stage"], found["group"]
    fields = service.fields_of(tpl)
    meta = EMAIL_STAGE_META.get(stage.value, {})
    out = {
        "key": key_of(tpl), "group": group, "label": meta.get("label"), "about": meta.get("note"),
        "name": fields.name, "subject": fields.subject, "enabled": bool(fields.enabled), "sends": sends(fields, stage),
        "version": tpl.version or 1, "design": design_out(fields.design),
    }
    if group == "review":
        out["words"] = {"event": "this event's own words", "club": "the club's words", "default": "MyOtherApp's words",
                        "library": "the club's library"}.get(found["source"], found["source"])
    else:
        out["audience"] = _audience(fields)
        out["attaches"] = [n for n, on in (("the guest's ticket", fields.attachments.ticket_pdf), ("their certificate", fields.attachments.certificate_pdf)) if on]
    if event is not None:
        path = f"/events/{event.id}/emails/review/{stage.value}" if group == "review" else f"/events/{event.id}/emails/{tpl.id}"
    else:
        path = f"/emails/{tpl.id}"
    out["dashboard_url"] = dashboard_url(path)
    return out


def _tokens(stage) -> list[dict]:
    from app.core.layout.tokens import TICKET_TOKENS
    from app.core.mail.schema import EMAIL_ONLY_TOKENS, REQUEST_TOKENS

    out = [{"key": t["key"], "label": t["label"]} for t in [*TICKET_TOKENS, *EMAIL_ONLY_TOKENS]]
    out += [{"key": t["key"], "label": t["label"]} for t in REQUEST_TOKENS if stage.value in t.get("stages", ())]
    return out


# ─── Changing one ────────────────────────────────────────────────────────


def _unknown_fields(args: dict) -> list[str]:
    """Fields the editor's shapes don't have (they would be dropped without a word)."""
    from app.core.mail import schema as mail

    models = {m.model_fields["kind"].default: m for m in (
        mail.HeadingBlock, mail.ParagraphBlock, mail.ButtonBlock, mail.ImageBlock, mail.DividerBlock, mail.SpacerBlock,
        mail.ListBlock, mail.DetailsBlock, mail.TicketCardBlock,
    )}

    def names(model) -> set:
        return {f.alias or n for n, f in model.model_fields.items()} | set(model.model_fields)

    out = []
    theme = args.get("theme")
    if isinstance(theme, dict):
        extra = sorted(set(theme) - names(mail.EmailTheme))
        if extra:
            out.append(f"theme: {', '.join(extra)} are not theme fields and were left out")
    for i, b in enumerate(args.get("blocks") or []):
        model = models.get(b.get("kind")) if isinstance(b, dict) else None
        if model is None:
            continue
        extra = sorted(set(b) - names(model))
        if isinstance(b.get("style"), dict):
            extra += [f"style.{k}" for k in sorted(set(b["style"]) - names(mail.BlockStyle))]
        if extra:
            out.append(f"blocks.{i} ({b.get('kind')}): {', '.join(extra)} left out")
    return out


def _say(e: ValidationError) -> list[str]:
    out = []
    for err in e.errors()[:6]:
        where = ".".join(str(p) for p in err.get("loc", ()) if not (isinstance(p, str) and p in (
            "heading", "paragraph", "button", "image", "divider", "spacer", "list", "details", "ticket_card")))
        out.append(f"{where or 'email'}: {err.get('msg', 'invalid').removeprefix('Value error, ')}")
    return out


def merged(found: dict, args: dict, *, allow_enable: bool = False):
    """The email's fields with the assistant's changes: subject, blocks,
    theme, name, switching off. Refused in words when they don't fit."""
    from app.api.emails import service
    from app.core.mail.schema import EmailTemplateFields

    tpl, group = found["tpl"], found["group"]
    data = service.fields_of(tpl).model_dump(by_alias=True)
    if "subject" in args:
        data["subject"] = args["subject"]
    if "name" in args:
        data["name"] = args["name"]
    if "theme" in args:
        from pydantic.alias_generators import to_camel

        # The editor's spelling (accentText), whichever the assistant used.
        data["design"]["theme"] = {**data["design"]["theme"], **{to_camel(k): v for k, v in (args["theme"] or {}).items()}}
    if "blocks" in args:
        data["design"]["blocks"] = args["blocks"]
    if "enabled" in args:
        on = bool(args["enabled"])
        if group == "review":
            if not on:
                raise ToolError("A registration review email goes by itself as a registration moves; it can't be switched off.", "ALWAYS_ON")
        elif on and not tpl.enabled and not allow_enable:
            raise ToolError(
                "Switching an email on means guests are sent it by itself, so it is done in the dashboard: "
                f"{dashboard_url(f'/events/{tpl.event_id}/emails' if tpl.event_id else '/emails')}",
                "SWITCH_ON_IN_DASHBOARD", {"dashboard_url": dashboard_url(f"/events/{tpl.event_id}/emails" if tpl.event_id else "/emails")},
            )
        data["enabled"] = on
    try:
        return EmailTemplateFields.model_validate(data)
    except ValidationError as e:
        problems = _say(e)
        raise ToolError("This email has a problem: " + "; ".join(problems), "INVALID_EMAIL", {"problems": problems})


def unsaved(found: dict, args: dict):
    """What a preview or a test draws: the saved email, with any changes given (unsaved)."""
    return merged(found, {k: v for k, v in args.items() if k in EDITS})


# ─── Reads ───────────────────────────────────────────────────────────────


async def _club_event(call: Call, event_id: int) -> Events:
    return await _event(call, event_id, load=(joinedload(Events.club),))


@tool(
    "list_event_emails",
    title="List an event's emails",
    scope="emails:read",
    description=(
        "The emails an event sends its guests: the four journey emails (invite = the ticket email, the day before, "
        "the reminder as the doors open, the thank-you) with whether each is on, when it goes and when it last went; "
        "its custom emails; and, for an event that reviews registrations, the six registration review emails with "
        "whose words they use and how many people sending one again would reach. Each has a key for the other tools."
    ),
    schema=obj({"event_id": EVENT_ID}, ("event_id",)),
)
async def list_event_emails(call: Call, args: dict) -> dict:
    from app.api.emails import review, service

    event = await _club_event(call, args["event_id"])
    data = await service.journey(call.session, event)
    journey = []
    for s in data["journey"]:
        tpl, last = s["template"], s["last_message"]
        fields = service.fields_of(tpl)
        journey.append({
            "key": key_of(tpl), "label": s["label"], "about": s["description"], "subject": fields.subject,
            "enabled": bool(tpl.enabled), "sends": sends(fields, tpl.stage), "next_at": _iso(s["due_at"]),
            "last_sent": {"at": _iso(last.created_at), "recipients": last.recipients, "sent": last.sent_count, "status": last.status.value} if last else None,
        })
    custom = [
        {"key": key_of(t), "name": t.name, "subject": t.subject, "enabled": bool(t.enabled), "sends": sends(service.fields_of(t), t.stage)}
        for t in data["custom"]
    ]
    rows = await review.overview(call.session, event)
    await call.session.commit()
    reviewed = [
        {
            "key": r["stage"].lower(), "label": r["label"], "when": r["when"], "subject": r["template"]["subject"],
            "words": {"event": "this event's own words", "club": "the club's words", "default": "MyOtherApp's words"}.get(r["source"], r["source"]),
            "can_send_again": bool(r["resendable"]), "would_reach": r["eligible"],
            "last_sent_again": {"at": _iso(r["last_resend"]["created_at"]), "people": r["last_resend"]["recipients"]} if r["last_resend"] else None,
        }
        for r in rows
    ]
    return {
        "event_id": event.id, "event": event.name, "journey": journey, "custom": custom,
        "review": {"in_use": bool(event.requires_approval), "emails": reviewed},
        "dashboard_url": dashboard_url(f"/events/{event.id}/emails"),
    }


@tool(
    "get_event_email",
    title="Get one of an event's emails",
    scope="emails:read",
    description=(
        "One of an event's emails, as the editor holds it: its subject, its design (theme and blocks), whether it is "
        "on, when it goes, to whom, what it attaches, and the tokens its text can use. Change it with update_event_email."
    ),
    schema=obj({"event_id": EVENT_ID, "key": KEY}, ("event_id", "key")),
)
async def get_event_email(call: Call, args: dict) -> dict:
    event = await _club_event(call, args["event_id"])
    found = await _event_email(call, event, args["key"])
    await call.session.commit()
    return {"event_id": event.id, "event": event.name, **email_out(found, event=event), "tokens": _tokens(found["stage"])}


@tool(
    "preview_event_email",
    title="Preview one of an event's emails",
    scope="emails:read",
    description=(
        "One of an event's emails as a guest would read it, filled in for the sample guest (never a real one): the "
        "subject and the text, and anything wrong with its tokens. Give subject, blocks or theme to preview changes "
        "without saving them; include_html adds the HTML."
    ),
    schema=obj({"event_id": EVENT_ID, "key": KEY, **EDITS,
                "include_html": {"type": "boolean", "default": False, "description": "Also return the email's HTML."}}, ("event_id", "key")),
)
async def preview_event_email(call: Call, args: dict) -> dict:
    from app.api.emails import service

    event = await _club_event(call, args["event_id"])
    found = await _event_email(call, event, args["key"])
    fields = unsaved(found, args)
    try:
        out = await service.preview(call.session, event, fields.subject, fields.design)
    except CustomHTTPException as e:
        raise refused(e)
    await call.session.commit()
    return _preview_out(found, out, args, ignored=_unknown_fields(args))


def _preview_out(found: dict, out: dict, args: dict, *, ignored: list) -> dict:
    result = {
        "key": key_of(found["tpl"]), "subject": out["subject"], "text": out["text"], "problems": out["problems"],
        "guest": "the sample guest; a real guest's details are never used here",
        "unsaved_changes": any(k in args for k in EDITS),
    }
    if ignored:
        result["ignored"] = ignored
    if args.get("include_html"):
        result["html"] = out["html"]
    return result


@tool(
    "list_library_emails",
    title="List the club's email library",
    scope="emails:read",
    description=(
        "The club's email library: the journey emails and registration review emails events start from (an event "
        "copies the journey emails the first time its emails are opened or sent, so a change here reaches the events "
        "that have not copied them yet), and the club's own custom emails. Each with its key, subject and whether it is on."
    ),
    schema=obj({}),
)
async def list_library_emails(call: Call, args: dict) -> dict:
    from app.api.emails import service
    from app.core.mail.schema import EMAIL_STAGE_META

    library = await service.ensure_library(call.session, call.club.id)
    await call.session.commit()
    return {
        "emails": [
            {"key": key_of(t), "label": EMAIL_STAGE_META.get(t.stage.value, {}).get("label"), "name": t.name, "subject": t.subject,
             "enabled": bool(t.enabled), "sends": sends(service.fields_of(t), t.stage)}
            for t in library
        ],
        "note": "An event that already has its copies of the journey emails keeps them; change those with update_event_email.",
        "dashboard_url": dashboard_url("/emails"),
    }


@tool(
    "get_library_email",
    title="Get a library email",
    scope="emails:read",
    description="One email of the club's library, as the editor holds it: subject, design (theme and blocks), whether it is on, when it goes, and its tokens.",
    schema=obj({"key": KEY}, ("key",)),
)
async def get_library_email(call: Call, args: dict) -> dict:
    found = await _library_email(call, args["key"])
    await call.session.commit()
    return {**email_out(found), "tokens": _tokens(found["stage"])}


@tool(
    "preview_library_email",
    title="Preview a library email",
    scope="emails:read",
    description="A library email as a guest would read it, for a sample event and the sample guest: subject, text and token problems. Give subject, blocks or theme to preview changes unsaved.",
    schema=obj({"key": KEY, **EDITS, "include_html": {"type": "boolean", "default": False}}, ("key",)),
)
async def preview_library_email(call: Call, args: dict) -> dict:
    from app.api.emails import service

    found = await _library_email(call, args["key"])
    fields = unsaved(found, args)
    try:
        out = await service.preview(call.session, service.sample_event(call.club), fields.subject, fields.design, club_id=call.club.id)
    except CustomHTTPException as e:
        raise refused(e)
    await call.session.commit()
    return _preview_out(found, out, args, ignored=_unknown_fields(args))


# ─── Writes ──────────────────────────────────────────────────────────────

CHANGE = {
    **EDITS,
    "name": {"type": "string", "minLength": 1, "maxLength": 80, "description": "The email's name in the dashboard."},
    "enabled": {"type": "boolean", "description": "false switches the email off. Switching one on is done in the dashboard."},
}


async def _save(call: Call, found: dict, fields, *, event: Optional[Events]) -> object:
    """The dashboard's save: an event's review email makes or changes the
    event's own copy; any other email is changed where it is."""
    from app.api.emails import review, service

    try:
        if found["group"] == "review" and event is not None:
            return await review.save(call.session, event, found["stage"], fields, created_by=call.principal.user.id)
        return await service.update_template(call.session, found["tpl"], fields)
    except CustomHTTPException as e:
        raise refused(e)


def _changed(args: dict) -> list[str]:
    return [k for k in ("subject", "blocks", "theme", "name", "enabled") if k in args]


@tool(
    "update_event_email",
    title="Change one of an event's emails",
    scope="emails:write",
    description=(
        "Change one of an event's emails: its subject, its blocks (all of them, replaced), its theme (the fields "
        "given), its name, or switch it off. Checked as the dashboard checks a save (tokens, block shapes, pictures). "
        "Saved, guests get the new words from the next send on; a registration review email gets this event's own "
        "copy. Switching an email on, its timer, its audience and its attachments are changed in the dashboard."
    ),
    schema=obj({"event_id": EVENT_ID, "key": KEY, **CHANGE}, ("event_id", "key")),
    read_only=False,
)
async def update_event_email(call: Call, args: dict) -> dict:
    event = await _club_event(call, args["event_id"])
    found = await _event_email(call, event, args["key"])
    changed = _changed(args)
    if not changed:
        raise ToolError("Give at least one of subject, blocks, theme, name or enabled.", "INVALID_ARGUMENT")
    fields = merged(found, args)
    before = {"subject": found["tpl"].subject, "version": found["tpl"].version or 1, "enabled": bool(found["tpl"].enabled), "words": found["source"]}
    tpl = await _save(call, found, fields, event=event)
    await _record(
        call, "mcp.email.updated", entity_type="email_template", entity_id=tpl.id, tool="update_event_email",
        summary=f"Email “{tpl.name}” of “{event.name}” changed by {call.principal.client_name}",
        event_id=event.id, key=key_of(tpl), fields=changed, before=before,
    )
    await call.session.commit()
    saved = {"tpl": tpl, "group": found["group"], "stage": tpl.stage, "source": "event"}
    return {"event_id": event.id, "saved": True, "changed": changed, **email_out(saved, event=event),
            "ignored": _unknown_fields(args), "next_step": "Check it with preview_event_email, or send_test_email to see it in your inbox."}


@tool(
    "update_library_email",
    title="Change a library email",
    scope="emails:write",
    description=(
        "Change one email of the club's library: subject, blocks, theme, name, or switch it off. Events that have not "
        "copied their journey emails yet start from it; events that have keep theirs. Checked as the dashboard checks a save."
    ),
    schema=obj({"key": KEY, **CHANGE}, ("key",)),
    read_only=False,
)
async def update_library_email(call: Call, args: dict) -> dict:
    found = await _library_email(call, args["key"])
    changed = _changed(args)
    if not changed:
        raise ToolError("Give at least one of subject, blocks, theme, name or enabled.", "INVALID_ARGUMENT")
    fields = merged(found, args)
    before = {"subject": found["tpl"].subject, "version": found["tpl"].version or 1, "enabled": bool(found["tpl"].enabled)}
    tpl = await _save(call, found, fields, event=None)
    await _record(
        call, "mcp.email.library_updated", entity_type="email_template", entity_id=tpl.id, tool="update_library_email",
        summary=f"Library email “{tpl.name}” changed by {call.principal.client_name}", key=key_of(tpl), fields=changed, before=before,
    )
    await call.session.commit()
    return {"saved": True, "changed": changed, **email_out({**found, "tpl": tpl}), "ignored": _unknown_fields(args)}


# ─── A test, to the person themselves ────────────────────────────────────


@lru_cache(maxsize=4)
def _limits(text: str):
    try:
        return tuple(parse_many(text))
    except ValueError:
        logger.error("APP_MCP_TEST_EMAIL_LIMIT %r is not a rate limit; using 3 per 10 minutes;10/day", text)
        return tuple(parse_many("3 per 10 minutes;10/day"))


def test_limited(address: str) -> Optional[int]:
    """Seconds to wait when this address has had its test emails; else None (and one is counted)."""
    from app.core.rate_limit import limiter

    store = limiter.limiter
    for item in _limits(settings.MCP_TEST_EMAIL_LIMIT or "3 per 10 minutes;10/day"):
        if not store.hit(item, "mcp-test-email", address):
            reset = store.get_window_stats(item, "mcp-test-email", address).reset_time
            return max(1, math.ceil(reset - time.time()))
    return None


def own_address(call: Call) -> Optional[str]:
    """The person who connected the assistant: a team member's own address, else the club account's."""
    p = call.principal
    address = p.member.email if p.member is not None else getattr(p.user, "email", None)
    return (address or "").strip() or None


@tool(
    "send_test_email",
    title="Send a test email to yourself",
    scope="emails:write",
    description=(
        "Send one of an event's emails, filled in for the sample guest, to the address of the person who connected "
        "this assistant -- and only there; its subject starts with [Test]. Give subject, blocks or theme to test "
        "changes before saving them. A few an hour."
    ),
    schema=obj({"event_id": EVENT_ID, "key": KEY, **EDITS}, ("event_id", "key")),
    read_only=False,
    idempotent=False,
)
async def send_test_email(call: Call, args: dict) -> dict:
    from app.api.emails import service
    from app.core.mail.schema import EmailAttachments

    event = await _club_event(call, args["event_id"])
    found = await _event_email(call, event, args["key"])
    fields = unsaved(found, args)
    to = own_address(call)
    if not to:
        raise ToolError("Your sign-in has no email address to send a test to.", "NO_ADDRESS")
    problems = service.design_problems(fields.subject, fields.design)
    if problems:
        raise ToolError(f"Fix this first: {problems[0]}", "INVALID_EMAIL", {"problems": problems})
    wait = test_limited(to.lower())
    if wait is not None:
        raise ToolError(f"That is enough test emails for now. Try again in {wait} seconds.", "TEST_EMAIL_LIMITED", {"retry_after": wait})
    await _record(
        call, "mcp.email.test_sent", entity_type="email_template", entity_id=found["tpl"].id, tool="send_test_email",
        summary=f"Test of “{found['tpl'].name}” of “{event.name}” sent to the connected person by {call.principal.client_name}",
        event_id=event.id, key=key_of(found["tpl"]), to=to,
    )
    try:
        out = await service.send_test(call.session, event, to=to, subject=fields.subject, design=fields.design, attachments=EmailAttachments())
    except CustomHTTPException as e:
        raise refused(e)
    return {"sent": True, "to": out["to"], "subject": f"[Test] {out['subject']}", "guest": "the sample guest",
            "note": "Sent only to you. Nothing went to any guest."}


# ─── Sending a review email again: a person approves ─────────────────────


def _guest_requests_on() -> bool:
    return bool(settings.MCP_GUEST_REQUESTS_ENABLED)


@tool(
    "request_resend_review_email",
    title="Ask to send a review email again",
    scope="emails:write",
    description=(
        "Ask a person to send one of an event's registration review emails again, in its current words, to the people "
        "it already went to (say, after fixing a mistake in it). Nothing is sent now: this returns a confirm_url for "
        "someone on the club's team to open in the dashboard and approve within 24 hours."
    ),
    schema=obj({"event_id": EVENT_ID, "key": {**KEY, "description": "A registration review email: request_received, request_approved, "
                                                                    "request_approved_pay, request_declined or request_lapsed."}},
               ("event_id", "key")),
    read_only=False,
    idempotent=False,
    available=_guest_requests_on,
)
async def request_resend_review_email(call: Call, args: dict) -> dict:
    from app.api.emails import review
    from app.api.mcp import actions
    from app.core.mail.schema import EMAIL_STAGE_META

    event = await _club_event(call, args["event_id"])
    try:
        stage = review.stage_of(args["key"])
    except CustomHTTPException:
        raise _not_found(args["key"])
    if stage not in review.RESENDABLE:
        raise ToolError("This email goes once, by itself, and can't be sent again.", "NOT_RESENDABLE")
    if not event.requires_approval:
        raise ToolError("These emails only go out for events that review registrations.", "NOT_REVIEWED")
    reach = (await review.count(call.session, event, stage))["eligible"]
    if not reach:
        raise ToolError("Nobody to send this to right now.", "NOBODY")
    row, token = await actions.request(
        call.session, call.principal, kind="resend_review_email", target_type="event", target_id=f"{event.id}:{stage.value}",
        payload={"event_id": event.id, "name": event.name, "stage": stage.value, "label": EMAIL_STAGE_META[stage.value]["label"], "would_reach": reach},
        request=call.request,
    )
    await call.session.commit()
    return {**actions.requested_out(row, token), "would_reach": reach}
