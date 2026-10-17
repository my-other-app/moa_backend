"""One ticket as a file -- for the download, the email and the bundle alike.

The download endpoint used to render inline, so there was nothing a worker
sending the same ticket by email could call. The rendering lives here now;
the endpoint, the delivery worker and the ZIP bundle all ask for the same
document and get the same bytes.
"""
import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from app.api.events.documents import (
    apply_layers,
    document_values,
    in_layout_mode,
    layout_values,
    pdf_page_options,
    qr_png_base64,
    render_layout_document,
)
from app.api.templates_audience import RegistrationRole
from app.core.utils.pdf import generate_pdf_bytes

logger = logging.getLogger(__name__)


@dataclass
class Document:
    content: bytes
    filename: str
    # The layered design's version at render time, so a later redesign can
    # say which tickets still carry the old look. 0 for an HTML design.
    design_version: int


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
    """The event poster, for an `event_icon` image layer."""
    poster = getattr(event, "poster", None)
    if isinstance(poster, dict):
        return poster.get("original") or poster.get("medium")
    return poster if isinstance(poster, str) else None


def design_version_of(style: dict) -> int:
    if not in_layout_mode(style):
        return 0
    return int((style.get("layout") or {}).get("version", 0) or 0)


def ticket_payload(registration) -> dict:
    """What the HTML ticket template reads."""
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
        "qr_code": qr_png_base64(registration.ticket_id),
    }


def _ticket_render(registration, style: dict):
    """Everything the ticket needs from the registration, read here on the
    request's own thread, and a call that only renders. The render (a PDF
    drawn by the layout renderer or wkhtmltopdf) is slow CPU work; split off
    like this it can run on a worker thread without the database ever being
    touched from there. `registration.event` and its club must be loaded."""
    event = registration.event
    if in_layout_mode(style):
        # Drawn by the layout renderer with the editor's own fonts and layout
        # code, so the ticket is what the canvas showed. No HTML, no browser.
        values = layout_values(registration, event, "ticket")
        ticket_id = registration.ticket_id
        icon, club = poster_url(event), getattr(event, "club", None)
        return lambda: render_layout_document(
            style, values, ticket_id, title=f"Ticket {ticket_id}", event_icon_url=icon, club=club,
        )

    payload = ticket_payload(registration)
    # Layer tokens are filled from the one shared answer. This used to read
    # them off the payload under keys it did not have, so the attendee's name
    # was blank on every layer-mode ticket.
    style = apply_layers(style, document_values(registration, event))
    context = {
        "email_payload": payload,
        "style": style,
        "qr_code_url": f"data:image/png;base64,{payload['qr_code']}",
    }
    # In layer mode the page is the artwork's own size, so the design fills
    # the PDF instead of sitting shrunk on an A4 sheet.
    options = pdf_page_options(style)
    return lambda: generate_pdf_bytes(template_path="events/ticket.html", context=context, options=options).getvalue()


def render_ticket_pdf(registration, style: dict) -> bytes:
    """The ticket PDF from a resolved design: the layer renderer when the
    design has layers, the HTML template otherwise. Blocks while it renders;
    async code uses ticket_document, which renders on a worker thread."""
    return _ticket_render(registration, style)()


async def ticket_document(session, registration) -> Document:
    """The ticket this registration is issued, from the design it resolves to."""
    from app.api.ticket_templates.service import get_template_for_event

    style = await get_template_for_event(session, registration.event_id, role=role_of(registration))
    # Rendered off the event loop: drawn inline, a burst of registrations
    # held every other request in the process behind each PDF in turn.
    render = _ticket_render(registration, style)
    return Document(
        content=await asyncio.to_thread(render),
        filename=f"ticket-{registration.ticket_id}.pdf",
        design_version=design_version_of(style),
    )
