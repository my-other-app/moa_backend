import logging
from typing import List, Optional, Union
from fastapi import BackgroundTasks

from app.core.email.email import send_email_with_attachment
from app.core.utils.pdf import generate_pdf_bytes

logger = logging.getLogger(__name__)


def send_payment_confirmation_email(
    subject: str,
    payload: dict,
    recipients: List[str],
    background_tasks: BackgroundTasks | None = None,
):
    def send_email(
        subject: str,
        payload: dict,
        recipients: List[str],
    ):
        logger.info(f"Sending payment confirmation email to {recipients}")
        pdf_bytes = generate_pdf_bytes(
            template_path="payments/receipt.html",
            context=payload,
        )
        response = send_email_with_attachment(
            sender_name="Finnet Payments",
            # No explicit sender: send_email_with_attachment falls back to
            # settings.SES_DEFAULT_SENDER. This used to hardcode
            # payments@finnet.app, which stopped matching the verified SES
            # identity after the move to myotherapp.in — and because this runs
            # in a background task, a rejected sender fails silently.
            recipients=recipients,
            subject=subject,
            html_template_path="payments/payment_receipt.email",
            template_context=payload,
            attachment_bytes=pdf_bytes,
            attachment_filename=f"finnet-payment-receipt.pdf",
        )
        return response

    if background_tasks:
        background_tasks.add_task(send_email, subject, payload, recipients)
    else:
        send_email(subject, payload, recipients)
