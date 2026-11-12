"""One certificate as a file, and whether a guest has one.

A certificate used to be implicit: anyone who checked in could download
one. It is now issued -- by the organiser, to whoever they choose, numbered,
and revocable. Nothing exists before that: a check-in is attendance, not a
certificate, and an event whose club issues none has none (`availability`).
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
from app.core.public_urls import site_url
from app.core.storage.bucket import key_from_url
from app.core.utils.pdf import generate_pdf_bytes

CERTIFICATE_PREFIX = "CERT"


def number_of(registration) -> str:
    """The number this guest's certificate carries: its own, drawn on its
    own (certificate_no). It used to fall back to the ticket id under CERT-
    for a registration older than the column, so a certificate shown to
    anyone gave away the code the door scans; those were all drawn a number
    of their own (migration certificate_public_ids), and issuing draws one
    for any still without. Never made from the ticket id."""
    return getattr(registration, "certificate_no", None) or ""


def public_id_of(registration) -> Optional[str]:
    """The certificate's public id, which its page and links are keyed by;
    None before it is issued."""
    return getattr(registration, "certificate_public_id", None) or None


def public_url_of(registration) -> Optional[str]:
    """The certificate's page on the public site, for sharing: by its public
    id, never the ticket id. None while it has none."""
    from app.core.public_urls import certificate_url

    public_id = public_id_of(registration)
    return certificate_url(public_id) if public_id else None


def qr_text(registration) -> str:
    """The public verifier's page for this number: a phone's camera opens it.
    It used to be `certificate:<ticket id>`, the ticket the door scans."""
    return site_url(f"/verify/{number_of(registration)}")


@dataclass
class Availability:
    available: bool
    reason: str  # "issued" | "revoked" | "not_issued"


def availability(registration) -> Availability:
    """Whether this guest holds a certificate.

    Issued and not revoked is the whole rule. A certificate exists because
    the organiser issued it (POST /events/{id}/certificates/issue), to the
    audience they chose -- never because someone walked through the door.

    Checking in used to be enough on its own, so an event whose club had
    issued nothing handed a certificate to everyone who checked in, the
    moment they checked in. Certificates are for some events only, and who
    gets one is the club's decision: until it is taken, there is nothing to
    see, nothing to download and nothing to verify.
    """
    if getattr(registration, "certificate_revoked_at", None):
        return Availability(False, "revoked")
    if getattr(registration, "certificate_issued_at", None):
        return Availability(True, "issued")
    return Availability(False, "not_issued")


def certificate_context(registration, style: dict, *, qr: bool = True) -> dict:
    """The HTML template's context, shared by the PDF and the PNG. Without
    `qr` the QR images are left for the render to add (`with_qr`): drawing
    them is CPU work, and a bundle prepares hundreds of these on the loop."""
    event = registration.event
    issued = getattr(registration, "certificate_issued_at", None) or datetime.now(timezone.utc)
    # Nothing of the ticket: a certificate is shown to anyone, and the ticket
    # id is what the door scans. A design's "ticket code" layer prints the
    # certificate's number, as the layout designs' does (layout_values).
    payload = {
        "certificate_no": number_of(registration),
        "participant_name": registration.full_name,
        "event_name": event.name,
        "event_date": event.event_datetime.strftime("%d %b %Y"),
        "issued_date": issued.strftime("%d %b %Y"),
        "organizer_name": event.club.name if event.club else "",
    }
    context = {
        "email_payload": payload,
        "style": apply_layers(style, {**document_values(registration, event), "ticket_code": number_of(registration)}),
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
    """The certificate's render, prepared on the loop (see tickets.ticket_job).
    `style` is the design it is drawn from: for an issued one, its pin
    (certificate_style, doc_presets.pins)."""
    event = registration.event
    version = design_version_of(style)
    from app.api.doc_presets.config import preset_of, without_preset

    if preset_of(style) is not None:
        # One of the designer's designs; the classic certificate when the
        # render service cannot draw it (doc_presets.jobs). Revoking it is
        # part of its name, as its availability is (the ETag seeds it too).
        from app.api.doc_presets.jobs import preset_job

        return preset_job(
            "certificate", registration, style, fallback=certificate_job(registration, without_preset(style)),
            revoked_at=getattr(registration, "certificate_revoked_at", None),
        )
    if in_layout_mode(style):
        values = layout_values(registration, event, "certificate")
        qr = qr_text(registration)
        icon = poster_url(event)
        # The PDF's title travels with the file: the number, never the ticket id.
        title = f"Certificate {number_of(registration)}"

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


async def certificate_style(session, registration) -> dict:
    """The design this registration's certificate is drawn from: the one it
    was issued with (contract §6: doc_presets.pins), else the event's current
    one for its role."""
    from app.api.certificate_templates.service import get_template_for_event
    from app.api.doc_presets.pins import pinned_style

    return pinned_style(await get_template_for_event(session, registration.event_id, role=role_of(registration)), registration)


async def certificate_document(session, registration, style: Optional[dict] = None) -> Document:
    """The certificate as a PDF: drawn on the render pool, kept in the bucket
    (app.api.events.doc_cache) so a download, a resend and the bundle reuse it."""
    style = style if style is not None else await certificate_style(session, registration)
    job = certificate_job(registration, style)
    content = await doc_cache.cached(job.cache_key("certificate", registration.id), job.draw)
    return Document(
        content=content,
        filename=f"certificate-{number_of(registration)}.pdf",
        design_version=job.design_version,
    )


def current_design_version(style: dict) -> int:
    return design_version_of(style)
