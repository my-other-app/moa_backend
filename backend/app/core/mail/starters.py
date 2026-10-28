"""The four emails every event starts with, written to be sent as they are
and edited by anyone. The invite is the ticket email; the others fire on
the event's clock. Each is a complete template: subject, design and how it
is sent. Mirrors Muster's starters, minus the agenda block we do not have.

Below them, the six emails of an event that reviews registrations
(REQUEST_STARTERS): each club's library starts from them, and "Reset to
default" comes back to them (docs/APPROVALS.md §9)."""
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
    # rows empty: the card's own lines (event, when, where, ticket code), which is what every starter wants.
    return {
        "kind": "ticket_card", "id": _id("t"), "showQr": True, "showVenue": show_venue, "eyebrow": "{{ticket_type}}", "title": "{{attendee_name}}", "rows": [],
        "note": "Show this code at the door, on your phone or printed. If it will not scan, give the ticket code.", "style": _style(paddingBottom=16),
    }


def _qr() -> dict:
    return {"kind": "image", "id": _id("i"), "source": "qr", "imageKey": None, "alt": "Your ticket QR code", "width": 40, "href": None, "style": _style(align="center", paddingTop=8, paddingBottom=8)}


THEME = {
    "font": "sans", "background": "#F3F4F1", "surface": "#FFFFFF", "text": "#141614", "muted": "#6B716C",
    "accent": "brand", "accentText": "#FFFFFF", "radius": 12, "logo": "org_logo",
    "footer": "{{org_name}}{% if organiser_email %} · {{organiser_email}}{% endif %}\nYou are receiving this because you are on the guest list for {{event_name}}.",
}

CONTACT = "Questions? Reply to this email{% if organiser_email %} or write to {{organiser_email}}{% endif %}."
_QUIET = {"size": 14, "color": "#6B716C"}

# ─── An event that reviews every registration ────────────────────────────
#
# The six emails a registration under review gets (app/api/approvals), as
# MyOtherApp writes them: each club's library starts from these, the club
# edits the words, and "Reset to default" comes back here. The person
# registered (an RSVP); the club confirms some and declines the rest.

REQUEST_FOOTER = (
    "{{org_name}}{% if organiser_email %} · {{organiser_email}}{% endif %}\n"
    "You are receiving this because you registered for {{event_name}}."
)
REQUEST_THEME = {**THEME, "footer": REQUEST_FOOTER}


def _request(name: str, subject: str, blocks: list, *, ticket: bool = False) -> EmailTemplateFields:
    # Sent by the server as the registration moves: no timer, no audience,
    # always on. The ticket's own email carries the ticket.
    return EmailTemplateFields.model_validate({
        "name": name,
        "subject": subject,
        "design": {"theme": REQUEST_THEME, "blocks": blocks},
        "attachments": {"ticketPdf": ticket, "certificatePdf": False},
        "audience": {"who": "ALL", "roles": []},
        "trigger": {"mode": "MANUAL"},
        "channels": ["email"],
        "enabled": True,
    })


def _received() -> EmailTemplateFields:
    return _request("Thanks for registering", "Thanks for registering for {{event_name}}", [
        _heading("Thanks for registering, {{first_name}}."),
        _paragraph("Thank you for registering for **{{event_name}}**. {{org_name}} reviews every registration and will get back to you soon."),
        _paragraph("If yours is confirmed, your ticket will come by email."),
        _details([{"label": "When", "value": "{{event_start}}"}, {"label": "Where", "value": "{{venue}}"}]),
        _button("See your registration", "{{status_url}}"),
        _paragraph("Can't make it? [Cancel my registration]({{cancel_url}}).", **_QUIET),
    ])


def _approved() -> EmailTemplateFields:
    return _request("Confirmed · ticket", "Your ticket for {{event_name}}", [
        _heading("Your registration is confirmed."),
        _paragraph(
            "{{org_name}} confirmed your registration for **{{event_name}}**. Your ticket is attached and shown below. "
            "Keep this email: the QR code is what gets you through the door, on your phone or printed."
        ),
        _ticket_card(True),
        _details([{"label": "When", "value": "{{event_start}}"}, {"label": "Where", "value": "{{venue}}"}, {"label": "Ticket", "value": "{{ticket_type}} · {{ticket_code}}"}]),
        _button("Download your ticket", "{{ticket_url}}"),
        _paragraph(CONTACT, **_QUIET),
    ], ticket=True)


def _approved_pay() -> EmailTemplateFields:
    return _request("Confirmed · pay for the ticket", "Your registration for {{event_name}} is confirmed", [
        _heading("Your registration is confirmed, {{first_name}}."),
        _paragraph("{{org_name}} confirmed your registration for **{{event_name}}**. Pay {{pay_amount}} by {{pay_by}} to get your ticket."),
        _details([
            {"label": "Ticket", "value": "{{ticket_type}}"}, {"label": "To pay", "value": "{{pay_amount}}"},
            {"label": "Pay by", "value": "{{pay_by}}"}, {"label": "When", "value": "{{event_start}}"},
        ]),
        _button("Pay {{pay_amount}}", "{{pay_url}}"),
        _paragraph(
            "Your ticket is held for you until then. If it isn't paid by then, your registration expires. "
            "Can't make it? [Cancel my registration]({{cancel_url}}).",
            **_QUIET,
        ),
    ])


def _declined() -> EmailTemplateFields:
    return _request("Not confirmed", "Your registration for {{event_name}}", [
        _heading("About your registration, {{first_name}}"),
        _paragraph("Thank you for your interest in **{{event_name}}**. {{org_name}} couldn't confirm your registration this time."),
        # Only when the club wrote one; an empty paragraph is left out of the email.
        _paragraph("{% if decline_note %}A note from {{org_name}}: “{{decline_note}}”{% endif %}"),
        _paragraph("Thank you for registering.", **_QUIET),
    ])


def _lapsed() -> EmailTemplateFields:
    return _request("Expired · not paid in time", "Your registration for {{event_name}} has expired", [
        _heading("Your registration has expired."),
        _paragraph("{{org_name}} confirmed your registration for **{{event_name}}**, but it wasn't paid in time, so it has expired."),
        _paragraph("If you'd still like to go, [register again from the event's page]({{event_url}}).", **_QUIET),
    ])


def _unanswered() -> EmailTemplateFields:
    return _request("Closed before review", "Your registration for {{event_name}}", [
        _heading("About your registration, {{first_name}}"),
        _paragraph("{{org_name}} wasn't able to confirm your registration for **{{event_name}}**."),
        _paragraph("Your registration is closed. Nothing was charged. Thank you for your interest.", **_QUIET),
    ])


REQUEST_STARTERS = {
    "REQUEST_RECEIVED": _received,
    "REQUEST_APPROVED": _approved,
    "REQUEST_APPROVED_PAY": _approved_pay,
    "REQUEST_DECLINED": _declined,
    "REQUEST_LAPSED": _lapsed,
    "REQUEST_UNANSWERED": _unanswered,
}


def request_starter(stage: str) -> EmailTemplateFields:
    """MyOtherApp's default for one of the request emails."""
    global _n
    _n = 0
    return REQUEST_STARTERS[stage]()


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
                _details([{"label": "When", "value": "{{event_start}}"}, {"label": "Where", "value": "{{venue}}"}, {"label": "Ticket", "value": "{{ticket_type}} · {{ticket_code}}"}]),
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
                _paragraph("{{attendee_name}} · {{ticket_type}} · {{ticket_code}}", align="center", **_QUIET),
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
    if stage in REQUEST_STARTERS:
        return request_starter(stage)
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
