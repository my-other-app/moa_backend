"""One ticket as a file -- for the download, the email and the bundle alike.

The download endpoint used to render inline, so there was nothing a worker
sending the same ticket by email could call. The rendering lives here now;
the endpoint, the delivery worker and the ZIP bundle all ask for the same
document and get the same bytes.

Every render is split in two: what it needs is read from the registration on
the request's own loop (`ticket_job`), and the drawing runs on the render pool
(app.core.layout.pool) without touching the database. The PDF is then kept in
the bucket (app.api.events.doc_cache), so the next download or resend of the
same ticket is a read, not a render.
"""
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Callable, Optional

from app.api.events import doc_cache
from app.api.events.documents import (
    apply_layers,
    document_values,
    in_layout_mode,
    layout_colors,
    layout_values,
    pdf_page_options,
    qr_png_base64,
    render_layout,
)
from app.api.templates_audience import RegistrationRole
from app.api.templates_images import TICKET_FIELDS, embedded
from app.core.storage.bucket import key_from_url
from app.core.utils.pdf import generate_pdf_bytes

logger = logging.getLogger(__name__)


@dataclass
class Document:
    content: bytes
    filename: str
    # The layered design's version at render time, so a later redesign can
    # say which tickets still carry the old look. 0 for an HTML design.
    design_version: int


@dataclass
class RenderJob:
    """One document's render, prepared on the loop: `draw` touches no
    database, and `fingerprint` names everything it prints."""

    draw: Callable[[], tuple[bytes, bool]]
    fingerprint: str
    design_version: int
    style: dict

    def cache_key(self, kind: str, registration_id) -> Optional[str]:
        if registration_id is None:
            return None
        return doc_cache.key_for(kind, registration_id, self.design_version, self.fingerprint)


def role_of(registration) -> Optional[RegistrationRole]:
    """The role this registration's document should be rendered for.

    Every registration carries one -- the column is not nullable and defaults
    to attendee -- so None here is a fallback, not a normal state. It covers
    the rows that predate the column and any value this build does not
    recognise, and it resolves to the event's default design.
    """
    raw = getattr(registration, "role", None)
    if raw is None:
        return None
    value = getattr(raw, "value", raw)
    try:
        return RegistrationRole(value)
    except ValueError:
        # An unknown role in the database must not stop a ticket rendering.
        logger.warning("Unknown registration role %r, using the event default", value)
        return None


def poster_url(event) -> Optional[str]:
    """The event poster, for an `event_icon` image layer: the large or
    medium variant -- a few hundred kilobytes -- rather than the original,
    which can be a 10 MB phone photo read for a 120-pixel icon."""
    poster = getattr(event, "poster", None)
    if isinstance(poster, dict):
        return poster.get("large") or poster.get("medium") or poster.get("original")
    return poster if isinstance(poster, str) else None


def design_version_of(style: dict) -> int:
    if not in_layout_mode(style):
        return 0
    return int((style.get("layout") or {}).get("version", 0) or 0)


def ticket_payload(registration, *, qr: bool = True) -> dict:
    """What the HTML ticket template reads. Without `qr` the QR is left for
    the render to draw (it is CPU work; see ticket_job)."""
    event = registration.event
    event_endtime = (
        event.event_datetime + timedelta(hours=event.duration) if event.duration else None
    )
    return {
        "ticket_id": registration.ticket_id,
        "participant_name": registration.full_name,
        "event_name": event.name,
        "event_date": event.event_datetime.strftime("%d %b %Y"),
        "event_time": (
            event.event_datetime.strftime("%I:%M %p")
            + (" - " + event_endtime.strftime("%I:%M %p"))
            if event_endtime
            else ""
        ),
        "event_location": event.location_name,
        "event_prizes": f"Prizes worth ₹{event.prize_amount}" if event.prize_amount else None,
        "organizer_name": event.club.name,
        "contact_email": event.contact_email,
        "contact_phone": event.contact_phone,
        **({"qr_code": qr_png_base64(registration.ticket_id)} if qr else {}),
    }


def ticket_job(registration, style: dict) -> RenderJob:
    """Everything the ticket needs from the registration, read here on the
    request's own thread, and a call that only renders. The render (a PDF
    drawn by the layout renderer or wkhtmltopdf) is slow CPU work; split off
    like this it runs on a render thread without the database ever being
    touched from there. `registration.event` and its club must be loaded."""
    event = registration.event
    version = design_version_of(style)
    if in_layout_mode(style):
        # Drawn by the layout renderer with the editor's own fonts and layout
        # code, so the ticket is what the canvas showed. No HTML, no browser.
        values = layout_values(registration, event, "ticket")
        ticket_id = registration.ticket_id
        icon, club = poster_url(event), getattr(event, "club", None)

        def draw() -> tuple[bytes, bool]:
            return render_layout(style, values, ticket_id, title=f"Ticket {ticket_id}", event_icon_url=icon, club=club)

        print_ = doc_cache.fingerprint("ticket-layout", style, values, ticket_id, key_from_url(icon) or icon, layout_colors(style, club))
        return RenderJob(draw, print_, version, style)

    payload = ticket_payload(registration, qr=False)
    # Layer tokens are filled from the one shared answer. This used to read
    # them off the payload under keys it did not have, so the attendee's name
    # was blank on every layer-mode ticket.
    styled = apply_layers(style, document_values(registration, event))
    # In layer mode the page is the artwork's own size, so the design fills
    # the PDF instead of sitting shrunk on an A4 sheet.
    options = pdf_page_options(styled)

    def draw() -> tuple[bytes, bool]:
        # The pictures go in as data: URIs, so wkhtmltopdf fetches nothing.
        inline, complete = embedded(styled, fields=TICKET_FIELDS)
        qr = qr_png_base64(payload["ticket_id"])
        context = {
            "email_payload": {**payload, "qr_code": qr},
            "style": inline,
            "qr_code_url": f"data:image/png;base64,{qr}",
        }
        pdf = generate_pdf_bytes(template_path="events/ticket.html", context=context, options=options).getvalue()
        return pdf, complete

    print_ = doc_cache.fingerprint(
        "ticket-html", doc_cache.template_digest("events/ticket.html"),
        payload, styled, options,
    )
    return RenderJob(draw, print_, version, style)


def render_ticket_pdf(registration, style: dict) -> bytes:
    """The ticket PDF from a resolved design: the layer renderer when the
    design has layers, the HTML template otherwise. Blocks while it renders;
    async code uses ticket_document, which renders on the render pool."""
    return ticket_job(registration, style).draw()[0]


async def ticket_style(session, registration) -> dict:
    from app.api.ticket_templates.service import get_template_for_event

    return await get_template_for_event(session, registration.event_id, role=role_of(registration))


async def ticket_document(session, registration, style: Optional[dict] = None) -> Document:
    """The ticket this registration is issued, from the design it resolves to.

    Rendered off the event loop: drawn inline, a burst of registrations held
    every other request in the process behind each PDF in turn. Kept in the
    bucket once drawn, so the same ticket is never drawn twice."""
    style = style if style is not None else await ticket_style(session, registration)
    job = ticket_job(registration, style)
    content = await doc_cache.cached(job.cache_key("ticket", registration.id), job.draw)
    return Document(
        content=content,
        filename=f"ticket-{registration.ticket_id}.pdf",
        design_version=job.design_version,
    )
