"""One certificate as a file, and whether a guest has one.

A certificate used to be implicit: anyone who checked in could download
one. It is now issued -- by the organiser, to whoever they choose, numbered,
and revocable -- while the download an attended guest already had keeps
working until the organiser takes the decision in hand.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

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
from app.api.events.tickets import Document, RenderJob, design_version_of, poster_url, role_of
from app.api.templates_images import CERTIFICATE_FIELDS, embedded
from app.core.storage.bucket import key_from_url
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


def certificate_context(registration, style: dict, *, qr: bool = True) -> dict:
    """The HTML template's context, shared by the PDF and the PNG. Without
    `qr` the QR images are left for the render to add (`with_qr`): drawing
    them is CPU work, and a bundle prepares hundreds of these on the loop."""
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
    }
    context = {
        "email_payload": payload,
        "style": apply_layers(style, document_values(registration, event)),
        "qr_text": qr_text(registration),
    }
    return with_qr(context) if qr else context


def with_qr(context: dict) -> dict:
    """The context with its QR drawn in, as a PNG and as a data URL."""
    png = qr_png_base64(context["qr_text"])
    return {
        **context,
        "email_payload": {**context["email_payload"], "qr_code": png},
        "qr_code_url": f"data:image/png;base64,{png}",
    }


def certificate_job(registration, style: dict) -> RenderJob:
    """The certificate's render, prepared on the loop (see tickets.ticket_job)."""
    event = registration.event
    version = design_version_of(style)
    if in_layout_mode(style):
        values = layout_values(registration, event, "certificate")
        qr = qr_text(registration)
        icon = poster_url(event)
        title = f"Certificate {registration.ticket_id}"

        def draw() -> tuple[bytes, bool]:
            # The certificate's colours are its own design's (no club brand),
            # as they always were.
            return render_layout(style, values, qr, title=title, event_icon_url=icon)

        print_ = doc_cache.fingerprint("certificate-layout", style, values, qr, key_from_url(icon) or icon, layout_colors(style))
        return RenderJob(draw, print_, version, style)

    context = certificate_context(registration, style, qr=False)
    options = pdf_page_options(style, default={"orientation": "Landscape"})

    def draw() -> tuple[bytes, bool]:
        inline, complete = embedded(context["style"], fields=CERTIFICATE_FIELDS)
        pdf = generate_pdf_bytes(
            template_path="events/certificate.html",
            context={**with_qr(context), "style": inline},
            options=options,
        ).getvalue()
        return pdf, complete

    print_ = doc_cache.fingerprint(
        "certificate-html", doc_cache.template_digest("events/certificate.html"),
        context["email_payload"], context["qr_text"], context["style"], options,
    )
    return RenderJob(draw, print_, version, style)


def render_certificate_pdf(registration, style: dict) -> bytes:
    """Blocks while it renders; async code uses certificate_document."""
    return certificate_job(registration, style).draw()[0]


async def certificate_style(session, registration) -> dict:
    from app.api.certificate_templates.service import get_template_for_event

    return await get_template_for_event(session, registration.event_id, role=role_of(registration))


async def certificate_document(session, registration, style: Optional[dict] = None) -> Document:
    """The certificate as a PDF: drawn on the render pool, kept in the bucket
    (app.api.events.doc_cache) so a download, a resend and the bundle reuse it."""
    style = style if style is not None else await certificate_style(session, registration)
    job = certificate_job(registration, style)
    content = await doc_cache.cached(job.cache_key("certificate", registration.id), job.draw)
    return Document(
        content=content,
        filename=f"certificate-{registration.ticket_id}.pdf",
        design_version=job.design_version,
    )


def current_design_version(style: dict) -> int:
    return design_version_of(style)


def issued_at_of(registration) -> Optional[datetime]:
    return getattr(registration, "certificate_issued_at", None)
