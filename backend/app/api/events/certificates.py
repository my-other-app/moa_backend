"""One certificate as a file, and whether a guest has one.

A certificate used to be implicit: anyone who checked in could download
one. It is now issued -- by the organiser, to whoever they choose, numbered,
and revocable -- while the download an attended guest already had keeps
working until the organiser takes the decision in hand.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from app.api.events.documents import (
    apply_layers,
    document_values,
    in_layout_mode,
    layout_values,
    pdf_page_options,
    qr_data_url,
    qr_png_base64,
    render_layout_document,
)
from app.api.events.tickets import Document, design_version_of, poster_url, role_of
from app.core.utils.pdf import generate_pdf_bytes

CERTIFICATE_PREFIX = "CERT"


def certificate_number(ticket_id: str) -> str:
    """The certificate's number, in the shape of the ticket's and never
    readable as one: MOA-2BCEBA8-PFZG3 certifies as CERT-2BCEBA8-PFZG3."""
    body = ticket_id.split("-", 1)[1] if "-" in ticket_id else ticket_id
    return f"{CERTIFICATE_PREFIX}-{body}"


def ticket_id_of_certificate(number: str) -> str:
    """The ticket id a certificate number or verification code points at."""
    code = number.strip()
    if code.startswith("certificate:"):
        code = code[len("certificate:"):]
    if code.upper().startswith(f"{CERTIFICATE_PREFIX}-"):
        return "MOA-" + code[len(CERTIFICATE_PREFIX) + 1:]
    return code


def qr_text(registration) -> str:
    return f"certificate:{registration.ticket_id}"


@dataclass
class Availability:
    available: bool
    reason: str  # "issued" | "attended" | "revoked" | "not_issued"


def availability(registration) -> Availability:
    """Whether this guest holds a valid certificate.

    Issued and not revoked is the rule. A guest who checked in and was never
    explicitly issued one keeps the certificate they always had, so turning
    the feature on takes nothing away; issuing to anyone at an event moves
    that event onto the explicit rule for everyone else.
    """
    if getattr(registration, "certificate_revoked_at", None):
        return Availability(False, "revoked")
    if getattr(registration, "certificate_issued_at", None):
        return Availability(True, "issued")
    if registration.is_attended:
        return Availability(True, "attended")
    return Availability(False, "not_issued")


def certificate_context(registration, style: dict) -> dict:
    """The HTML template's context, shared by the PDF and the PNG."""
    event = registration.event
    issued = getattr(registration, "certificate_issued_at", None) or datetime.now(timezone.utc)
    payload = {
        "ticket_id": registration.ticket_id,
        "certificate_no": certificate_number(registration.ticket_id),
        "participant_name": registration.full_name,
        "event_name": event.name,
        "event_date": event.event_datetime.strftime("%d %b %Y"),
        "issued_date": issued.strftime("%d %b %Y"),
        "organizer_name": event.club.name if event.club else "",
        "qr_code": qr_png_base64(qr_text(registration)),
    }
    return {
        "email_payload": payload,
        "style": apply_layers(style, document_values(registration, event)),
        "qr_code_url": qr_data_url(qr_text(registration)),
    }


def render_certificate_pdf(registration, style: dict) -> bytes:
    if in_layout_mode(style):
        return render_layout_document(
            style,
            layout_values(registration, registration.event, "certificate"),
            qr_text(registration),
            title=f"Certificate {registration.ticket_id}",
            event_icon_url=poster_url(registration.event),
        )
    return generate_pdf_bytes(
        template_path="events/certificate.html",
        context=certificate_context(registration, style),
        options=pdf_page_options(style, default={"orientation": "Landscape"}),
    ).getvalue()


async def certificate_document(session, registration) -> Document:
    from app.api.certificate_templates.service import get_template_for_event

    style = await get_template_for_event(session, registration.event_id, role=role_of(registration))
    return Document(
        content=render_certificate_pdf(registration, style),
        filename=f"certificate-{registration.ticket_id}.pdf",
        design_version=design_version_of(style),
    )


def current_design_version(style: dict) -> int:
    return design_version_of(style)


def issued_at_of(registration) -> Optional[datetime]:
    return getattr(registration, "certificate_issued_at", None)
