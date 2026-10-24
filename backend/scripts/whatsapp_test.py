"""One approved WhatsApp template to one number, with sample values.

    $DC exec -T backend python -m scripts.whatsapp_test --to <number> --template ticket_confirmed

The check that the server's keys, the sending number and a template's
approval all work, before any guest is sent anything. It sends exactly one
message, to the number given -- there is no default, and it refuses to run
without one -- and prints Meta's message id, or Meta's reason in words
(app/core/whatsapp.py). The document templates carry a one-page sample PDF.

--to takes a number as people type it: 98765 43210 is +91 98765 43210, and
any other country needs its code (+44 ...). --template is one of the five
the code sends (ticket_confirmed, event_reminder, certificate_ready,
payment_pending, event_update); ticket_confirmed when left out.

Needs APP_WHATSAPP_ACCESS_TOKEN and APP_WHATSAPP_PHONE_NUMBER_ID (the
backend's own .env). The statuses of the message (delivered, read) arrive at
the WhatsApp hook if it is subscribed; they match no delivery row, which is
expected for a test.
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import sys
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

sys.path.insert(0, ".")

from app.core import whatsapp  # noqa: E402

TEMPLATES = ("ticket_confirmed", "event_reminder", "certificate_ready", "payment_pending", "event_update")
SAMPLE_EVENT = "MyOtherApp WhatsApp test"
SAMPLE_TICKET = "MOA-TEST0-00001"
SAMPLE_CERTIFICATE = "CERT-TEST0-00001"


def _when() -> str:
    """Tomorrow evening in India, written as the tickets write it (deliveries.service._when)."""
    at = (datetime.now(ZoneInfo("Asia/Kolkata")) + timedelta(days=1)).replace(hour=18, minute=30, second=0, microsecond=0)
    return at.strftime("%a %-d %b %Y, %-I:%M %p")


def sample(name: str) -> whatsapp.Template:
    if name == "ticket_confirmed":
        return whatsapp.ticket_confirmed(name="Test Guest", event=SAMPLE_EVENT, when=_when(), ticket_id=SAMPLE_TICKET)
    if name == "event_reminder":
        return whatsapp.event_reminder(name="Test Guest", event=SAMPLE_EVENT, when=_when(), venue="Innovation Hall", ticket_id=SAMPLE_TICKET)
    if name == "certificate_ready":
        return whatsapp.certificate_ready(name="Test Guest", event=SAMPLE_EVENT, club="MyOtherApp", number=SAMPLE_CERTIFICATE)
    if name == "payment_pending":
        return whatsapp.payment_pending(name="Test Guest", event=SAMPLE_EVENT, amount=whatsapp.rupees_text(499), slug="whatsapp-test")
    if name == "event_update":
        return whatsapp.event_update(name="Test Guest", club="MyOtherApp", event=SAMPLE_EVENT, update="this is a test of the update message")
    raise ValueError(f"unknown template {name!r}")


def sample_pdf(title: str) -> bytes:
    """A one-page PDF saying it is a sample."""
    from reportlab.lib.pagesizes import A6
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A6)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(24, 340, title)
    c.setFont("Helvetica", 10)
    c.drawString(24, 318, "A sample from scripts/whatsapp_test.py.")
    c.drawString(24, 302, "Not a ticket; admits nobody.")
    c.showPage()
    c.save()
    return buf.getvalue()


async def run(to: str, template_name: str, out=sys.stdout, err=sys.stderr) -> int:
    if not whatsapp.configured():
        print(
            "WhatsApp is not configured: set APP_WHATSAPP_ACCESS_TOKEN and APP_WHATSAPP_PHONE_NUMBER_ID "
            "in the server's .env and recreate the backend.",
            file=err,
        )
        return 2
    number, why = whatsapp.normalise(to)
    if number is None:
        print(f"--to {to!r} is not a number WhatsApp can reach: {why}", file=err)
        return 2
    template = sample(template_name)
    document: Optional[tuple[bytes, str]] = None
    if template.document:
        kind = "ticket" if template_name == "ticket_confirmed" else "certificate"
        document = (sample_pdf(f"Sample {kind}"), f"sample-{kind}.pdf")
    try:
        sent = await whatsapp.send(number, template, document=document)
    except (whatsapp.WhatsAppError, whatsapp.Deferred) as e:
        print(f"Not sent: {getattr(e, 'reason', None) or e}", file=err)
        return 1
    print(json.dumps({"sent": True, "template": template.name, "to": number, "message_id": sent["MessageId"]}), file=out)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m scripts.whatsapp_test", description="Send one WhatsApp template with sample values.")
    p.add_argument("--to", required=True, help="the number to send to (98765 43210, or +<country code>...)")
    p.add_argument("--template", default="ticket_confirmed", choices=TEMPLATES, help="which approved template (default ticket_confirmed)")
    args = p.parse_args(argv)
    return asyncio.run(run(args.to, args.template))


if __name__ == "__main__":
    sys.exit(main())
