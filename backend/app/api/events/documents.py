"""What a ticket or certificate says about the person it is issued to.

Three places rendered these documents and each built its own payload. The
ticket substituted layer tokens from keys the payload did not have, so the
attendee's name came out blank; the certificate never substituted them at
all, so every layer printed as literal braces; and the QR a certificate
layer pointed at was a variable its context never set.

One function now answers "what are this registration's values", and every
renderer -- PDF, image, and the designer's previews -- asks it. The token
names here are the ones the editor offers (TICKET_TOKENS and
CERTIFICATE_TOKENS in templates_shared), checked by a test, so a token the
editor can insert is a token the document will fill.
"""

import base64
import io
from datetime import timedelta
from typing import Optional

import qrcode

from app.api.templates_shared import render_layers

# Pixels in the editor are CSS pixels, which wkhtmltopdf lays out at 96dpi.
# A page sized from them in millimetres is the artwork at exactly 1:1.
_MM_PER_PX = 25.4 / 96

PLACINGS = {1: "First", 2: "Second", 3: "Third"}


def _time(dt) -> str:
    return dt.strftime("%I:%M %p").lstrip("0") if dt else ""


def document_values(registration, event=None) -> dict:
    """Every token either document can show, for this registration.

    Keys are the token names the editor offers. A value the event does not
    have is an empty string, not None: None would print as "None" through
    some paths and vanish through others, and an empty string is what a
    missing venue should look like on a ticket.
    """
    event = event or registration.event
    club = getattr(event, "club", None)
    start = event.event_datetime
    end = start + timedelta(hours=event.duration) if event.duration else None
    details = registration.additional_details or {}

    event_time = _time(start) + (f" - {_time(end)}" if end else "")

    return {
        "name": registration.full_name or "",
        "ticket_code": registration.ticket_id or "",
        "event_name": event.name or "",
        "event_title": event.name or "",
        "event_date": start.strftime("%d %b %Y"),
        "event_time": event_time,
        "event_start_time": _time(start),
        "event_end_time": _time(end),
        "venue": event.location_name or "",
        "club_name": (club.name if club else "") or "",
        # Registrations carry no team of their own; a custom field named
        # "team_name" on the event is where one lives when it exists.
        "team_name": str(details.get("team_name") or details.get("team") or ""),
        "certificate_id": registration.ticket_id or "",
        "placing": PLACINGS.get(registration.position or 0, ""),
    }


def sample_values(event, kind: str) -> dict:
    """Stand-in values for a design with no registration to preview.

    Real event details where the event has them, so the organiser sees the
    actual date and venue, and an obviously fictional person.
    """
    club = getattr(event, "club", None)
    start = event.event_datetime
    end = start + timedelta(hours=event.duration) if event.duration else None
    return {
        "name": "Aisha Rahman",
        "ticket_code": "MOA-SAMPLE-0041",
        "event_name": event.name or "",
        "event_title": event.name or "",
        "event_date": start.strftime("%d %b %Y"),
        "event_time": _time(start) + (f" - {_time(end)}" if end else ""),
        "event_start_time": _time(start),
        "event_end_time": _time(end),
        "venue": event.location_name or "",
        "club_name": (club.name if club else "") or "",
        "team_name": "Team Nova",
        "certificate_id": "MOA-SAMPLE-0041",
        "placing": "First" if kind == "certificate" else "",
    }


def qr_png_base64(text: str) -> str:
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(text)
    qr.make(fit=True)
    buffered = io.BytesIO()
    qr.make_image(fill_color="black", back_color="white").save(buffered, format="PNG")
    return base64.b64encode(buffered.getvalue()).decode("utf-8")


def qr_data_url(text: str) -> str:
    """The QR as a data URL, so a render needs no network to show it."""
    return f"data:image/png;base64,{qr_png_base64(text)}"


def in_layer_mode(style) -> bool:
    return isinstance(style, dict) and bool(style.get("layers"))


def canvas_size(style) -> tuple[int, int]:
    """The artwork's pixel size, with the renderer's own fallback.

    The editor seeds a real size before it saves a layer, so a saved design
    carries one. The fallback exists for a config written by hand.
    """
    canvas = (style or {}).get("canvas") or {}
    return int(canvas.get("width") or 1000), int(canvas.get("height") or 1000)


def apply_layers(style, values: dict):
    """Substitute tokens into a style's layers, if it has any."""
    if not in_layer_mode(style):
        return style
    return {**style, "layers": render_layers(style["layers"], values)}


def pdf_page_options(style, *, default: Optional[dict] = None) -> dict:
    """The page the PDF is printed on.

    In layer mode the page *is* the artwork: a 1560x690 ticket becomes a
    1560x690 page, so it fills the PDF edge to edge instead of sitting
    shrunk in the corner of an A4 sheet. The built-in layouts keep their
    paper sizes.
    """
    if not in_layer_mode(style):
        return dict(default or {})
    width, height = canvas_size(style)
    return {
        "page-width": f"{width * _MM_PER_PX:.2f}mm",
        "page-height": f"{height * _MM_PER_PX:.2f}mm",
        # Smart shrinking rescales content to the viewport; the page already
        # fits the artwork, so it would only blur the result.
        "disable-smart-shrinking": None,
    }


def image_width(style, *, default: int) -> int:
    """How wide to render the PNG: the artwork's width in layer mode."""
    if not in_layer_mode(style):
        return default
    return canvas_size(style)[0]
