"""The four emails every event starts with, written to be sent as they are
and edited by anyone. The invite is the ticket email; the others fire on
the event's clock. Each is a complete template: subject, design and how it
is sent. Mirrors Muster's starters, minus the agenda block we do not have."""
from app.core.mail.schema import EmailTemplateFields

_n = 0


def _id(kind: str) -> str:
    global _n
    _n += 1
    return f"{kind}-{_n}"


def _style(**more) -> dict:
    return {"align": "left", "color": None, "background": None, "size": None, "weight": None, "paddingTop": 0, "paddingBottom": 8, "letterSpacing": 0, **more}


def _heading(text: str, level: int = 1) -> dict:
    return {"kind": "heading", "id": _id("h"), "text": text, "level": level, "style": _style(paddingTop=8, paddingBottom=12)}


def _paragraph(text: str, **more) -> dict:
    return {"kind": "paragraph", "id": _id("p"), "text": text, "style": _style(paddingBottom=12, **more)}


def _button(label: str, href: str, variant: str = "primary") -> dict:
    return {"kind": "button", "id": _id("b"), "label": label, "href": href, "variant": variant, "style": _style(paddingTop=8, paddingBottom=20)}


def _details(rows: list[dict]) -> dict:
    return {"kind": "details", "id": _id("d"), "rows": rows, "style": _style(paddingBottom=16)}


def _ticket_card(show_venue: bool = True) -> dict:
    # rows empty: the card's own four lines, which is what every starter wants.
    return {"kind": "ticket_card", "id": _id("t"), "showQr": True, "showVenue": show_venue, "eyebrow": "{{ticket_type}}", "title": "{{attendee_name}}", "rows": [], "note": "Show this code at the door, on your phone or printed.", "style": _style(paddingBottom=16)}


def _qr() -> dict:
    return {"kind": "image", "id": _id("i"), "source": "qr", "imageKey": None, "alt": "Your ticket QR code", "width": 40, "href": None, "style": _style(align="center", paddingTop=8, paddingBottom=8)}


THEME = {
    "font": "sans", "background": "#F3F4F1", "surface": "#FFFFFF", "text": "#141614", "muted": "#6B716C",
    "accent": "brand", "accentText": "#FFFFFF", "radius": 12, "logo": "org_logo",
    "footer": "{{org_name}}{% if organiser_email %} · {{organiser_email}}{% endif %}\nYou are receiving this because you are on the guest list for {{event_name}}.",
}

CONTACT = "Questions? Reply to this email{% if organiser_email %} or write to {{organiser_email}}{% endif %}."
_QUIET = {"size": 14, "color": "#6B716C"}


def email_starter(stage: str) -> EmailTemplateFields:
    global _n
    _n = 0
    if stage == "INVITE":
        return EmailTemplateFields.model_validate({
            "name": "Invite · your ticket",
            "subject": "Your ticket for {{event_name}}",
            "design": {"theme": THEME, "blocks": [
                _heading("You’re in, {{first_name}}."),
                _paragraph("Your ticket for **{{event_name}}** is attached and shown below. Keep this email: the QR code is what gets you through the door, on your phone or printed."),
                _ticket_card(True),
                _details([{"label": "When", "value": "{{event_start}}"}, {"label": "Where", "value": "{{venue}}"}, {"label": "Ticket", "value": "{{ticket_type}} · {{admission_no}}"}]),
                _button("Download your ticket", "{{ticket_url}}"),
                _paragraph(CONTACT, **_QUIET),
            ]},
            "attachments": {"ticketPdf": True, "certificatePdf": False},
            "audience": {"who": "ALL", "roles": []},
            "trigger": {"mode": "ON_TICKETS"},
            "channels": ["email"],
            "enabled": True,
        })
    if stage == "PRE_EVENT":
        return EmailTemplateFields.model_validate({
            "name": "Pre-event · the day before",
            "subject": "Tomorrow: {{event_name}}",
            "design": {"theme": THEME, "blocks": [
                _heading("See you tomorrow, {{first_name}}."),
                _paragraph("**{{event_name}}** starts {{event_start}}{% if venue %} at {{venue}}{% endif %}. Doors open thirty minutes before."),
                _details([{"label": "When", "value": "{{event_start}}"}, {"label": "Where", "value": "{{venue}}"}, {"label": "Bring", "value": "Your QR code — below, or on your phone"}]),
                _ticket_card(False),
                _button("Download your ticket", "{{ticket_url}}", "secondary"),
                _paragraph(CONTACT, **_QUIET),
            ]},
            "attachments": {"ticketPdf": False, "certificatePdf": False},
            "audience": {"who": "ALL", "roles": []},
            "trigger": {"mode": "BEFORE_START", "minutes": 24 * 60},
            "channels": ["email"],
            "enabled": True,
        })
    if stage == "REMINDER":
        return EmailTemplateFields.model_validate({
            "name": "Reminder · doors are open",
            "subject": "Doors are open — {{event_name}}",
            "design": {"theme": THEME, "blocks": [
                _heading("Doors are open."),
                _paragraph("**{{event_name}}** begins in about {{doors_in}} minutes{% if venue %} at {{venue}}{% endif %}. Have your QR code ready at the door — it takes a second to scan.", align="center"),
                _qr(),
                _paragraph("{{attendee_name}} · {{ticket_type}} · {{admission_no}}", align="center", **_QUIET),
                _button("Open your ticket", "{{ticket_url}}", "secondary"),
            ]},
            "attachments": {"ticketPdf": False, "certificatePdf": False},
            "audience": {"who": "NOT_CHECKED_IN", "roles": []},
            "trigger": {"mode": "BEFORE_START", "minutes": 30},
            "channels": ["email"],
            "enabled": True,
        })
    if stage == "POST_EVENT":
        return EmailTemplateFields.model_validate({
            "name": "Post-event · thank you",
            "subject": "Thank you for coming to {{event_name}}",
            "design": {"theme": THEME, "blocks": [
                _heading("Thank you, {{first_name}}."),
                _paragraph("It was a pleasure to have you at **{{event_name}}**. {% if certificate_url %}Your certificate of attendance is ready: [download it here]({{certificate_url}}).{% endif %}"),
                _paragraph("We’d love to hear how it went — reply to this email with a line or two."),
                _paragraph("Until next time,\n{{org_name}}"),
            ]},
            "attachments": {"ticketPdf": False, "certificatePdf": True},
            "audience": {"who": "CHECKED_IN", "roles": []},
            # Sent by hand: what you want to say after an event depends on how
            # it went, and a thank-you that goes out two hours later, whatever
            # happened, is worse than one written the next morning.
            "trigger": {"mode": "MANUAL"},
            "channels": ["email"],
            "enabled": True,
        })
    return EmailTemplateFields.model_validate({
        "name": "New email",
        "subject": "A note about {{event_name}}",
        "design": {"theme": THEME, "blocks": [_heading("Hello {{first_name}},"), _paragraph("Write your message here."), _paragraph("{{org_name}}", **_QUIET)]},
        "attachments": {"ticketPdf": False, "certificatePdf": False},
        "audience": {"who": "ALL", "roles": []},
        "trigger": {"mode": "MANUAL"},
        "channels": ["email"],
        "enabled": True,
    })
