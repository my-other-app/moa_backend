import logging
from typing import List, Optional, Union
from fastapi import BackgroundTasks

from app.core.email.email import send_email_with_attachment
from app.core.utils.pdf import generate_pdf_bytes

logger = logging.getLogger(__name__)


def send_registration_confirmation_email(
    subject: str,
    payload: dict,
    recipients: List[str],
):
    logger.info(f"Sending registration confirmation email to {recipients}")
    # pdf_bytes = generate_pdf_bytes(
    #     template_path="events/ticket.html",
    #     context=payload,
    # )
    response = send_email_with_attachment(
        recipients=recipients,
        bcc_recipients=["muhammedr7025662019@gmail.com"],
        subject=subject,
        html_template_path="events/registration_confirmation.email",
        template_context=payload,
        sender_name="Myotherapp Events",
        sender="events@myotherapp.com",
        # attachment_bytes=pdf_bytes,
        # attachment_filename=f"{payload.get('ticket_id')}-ticket.pdf",
    )
    return response
