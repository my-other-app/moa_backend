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


# --- layered designs ------------------------------------------------------------
#
# A design made in the layer editor is drawn by app.core.layout, not by the
# HTML templates: the same font files and the same layout code the editor
# ran, so what the canvas showed is what prints. What follows is the bridge
# from our rows to that renderer's inputs.

import urllib.request
from datetime import datetime, timezone as _tz
from functools import lru_cache

from sqlalchemy import inspect as sa_inspect

from app.core.layout.model import TicketLayoutFields, ticket_area
from app.core.storage import objects
from app.core.layout.renderer import RenderInput, pdf_to_png, render_pdf
from app.core.layout.tokens import (
    TokenAttendee,
    TokenCertificate,
    TokenEvent,
    sample_token_values,
    ticket_token_values,
)

EVENT_TIMEZONE = "Asia/Kolkata"

ROLE_LABELS = {"attendee": "Attendee", "speaker": "Speaker", "staff": "Staff"}


def in_layout_mode(style) -> bool:
    """A design made in the layer editor, as opposed to the HTML templates."""
    return isinstance(style, dict) and bool((style.get("layout") or {}).get("layers"))


def layout_of(style) -> TicketLayoutFields:
    return TicketLayoutFields.model_validate(style["layout"])


def token_event(event) -> TokenEvent:
    start = event.event_datetime
    end = start + timedelta(hours=event.duration) if event.duration else start
    club = getattr(event, "club", None)
    return TokenEvent(
        name=event.name or "",
        code=f"E{event.id}",
        venue=event.location_name,
        organiser=club.name if club else None,
        starts_at=start,
        ends_at=end,
        timezone=EVENT_TIMEZONE,
    )


def field_labels_of(event) -> dict:
    """Custom field key → label, so an answer is reachable by either."""
    out = {}
    for f in event.additional_details or []:
        if isinstance(f, dict) and f.get("key") and f.get("label"):
            out[f["key"]] = f["label"]
    return out


def _loaded(obj, attr: str) -> bool:
    """Whether a relationship is already in memory.

    Reading one that is not would lazy-load it, and inside the async session
    that is a MissingGreenlet, not a query. The loaders fetch what each
    endpoint needs; anything else is simply not on the document.
    """
    try:
        return attr not in sa_inspect(obj).unloaded
    except Exception:
        return False


def token_attendee(registration) -> TokenAttendee:
    details = registration.additional_details if isinstance(registration.additional_details, dict) else {}
    org = None
    user = registration.user if _loaded(registration, "user") else None
    profile = user.profile if user is not None and _loaded(user, "profile") else None
    if profile is not None and _loaded(profile, "org") and profile.org is not None:
        org = profile.org.name
    return TokenAttendee(
        name=registration.full_name or "",
        organization=org,
        email=registration.email,
        seat_label=None,
        custom_fields=details,
        team=str(details.get("team_name") or details.get("team") or "") or None,
    )


def layout_values(registration, event, kind: str) -> dict:
    """Every token's value for this registration's ticket or certificate."""
    raw_role = getattr(registration, "role", None)
    role = getattr(raw_role, "value", raw_role) if raw_role else "attendee"
    certificate = None
    if kind == "certificate":
        from app.api.events.certificates import certificate_number

        certificate = TokenCertificate(
            number=certificate_number(registration.ticket_id or ""),
            issued_at=getattr(registration, "certificate_issued_at", None) or datetime.now(_tz.utc),
            checked_in_at=getattr(registration, "attended_on", None),
            placing=PLACINGS.get(registration.position or 0, ""),
        )
    club = getattr(event, "club", None)
    return ticket_token_values(
        token_event(event),
        (club.name if club else "") or "",
        token_attendee(registration),
        registration.ticket_id or "",
        ROLE_LABELS.get(str(role), str(role).title()),
        certificate=certificate,
        field_labels=field_labels_of(event),
    )


def layout_sample_values(event, kind: str) -> dict:
    labels = list(field_labels_of(event).values())
    club = getattr(event, "club", None)
    return sample_token_values(token_event(event), (club.name if club else "") or "", kind, fields=labels)


def layout_colors(style: dict) -> dict:
    """The brand colours a design may refer to, from the template's own palette."""
    primary = style.get("accent_color") or "#1db9a0"
    return {
        "primary": primary,
        "secondary": style.get("secondary_text_color") or "#aaaaaa",
        "ticketType": primary,
    }


@lru_cache(maxsize=64)
def fetch_bytes(url: str) -> Optional[bytes]:
    """An image the design points at. Cached per process: a club's logo is
    the same bytes on every ticket of the batch."""
    if not url:
        return None
    try:
        with urllib.request.urlopen(url, timeout=10) as r:  # noqa: S310 - our own storage URLs
            return r.read()
    except Exception:
        return None


def render_layout_document(style: dict, values: dict, qr_value: str, *, title: str,
                           event_icon_url: Optional[str] = None, skip_preprinted: bool = False) -> bytes:
    """The PDF of a layered design, for one set of values."""
    layout = layout_of(style)
    # A template uploaded in the designer is an object key; one set on the
    # template's artwork field is a URL. Either way the renderer gets bytes.
    template_key = layout.canvas.template_key
    template = objects.get_bytes(template_key) if template_key else fetch_bytes(style.get("background_image_url") or "")
    images = {
        l.image_key: objects.get_bytes(l.image_key)
        for l in layout.layers
        if l.kind == "image" and l.image_key
    }
    return render_pdf(RenderInput(
        title=title,
        canvas=layout.canvas,
        layers=layout.layers,
        page=layout.page,
        values=values,
        qr_value=qr_value,
        colors=layout_colors(style),
        template=template,
        images={k: v for k, v in images.items() if v},
        org_logo=fetch_bytes(style.get("logo_url") or ""),
        event_icon=fetch_bytes(event_icon_url or ""),
        skip_preprinted=skip_preprinted,
    ))


def layout_png(style: dict, pdf: bytes) -> bytes:
    """The PNG of that PDF, at the artwork's own width."""
    area = ticket_area(layout_of(style).canvas)
    return pdf_to_png(pdf, width_px=int(area.width))
