import os
import io
import logging
from typing import Any, List, Dict, Optional, Union
from jinja2 import Template
from fastapi import BackgroundTasks

from app.core.email import transport

logger = logging.getLogger(__name__)

def render_template(
    template_path: Optional[str] = None,
    template_str: Optional[str] = None,
    context: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Render a Jinja2 template from either a file path or a template string.

    :param template_path: Path to the template file
    :param template_str: Direct template string
    :param context: Context dictionary for template rendering
    :return: Rendered template string
    """
    if template_path:
        template_path = os.path.join("templates/email/", template_path)
        if not os.path.exists(template_path):
            raise FileNotFoundError(f"Template file not found: {template_path}")

        with open(template_path, "r", encoding="utf-8") as file:
            template_str = file.read()

    if not template_str:
        raise ValueError("Either template_path or template_str must be provided")

    template = Template(template_str, autoescape=True)
    return template.render(context or {})


def send_email_with_attachment(
    recipients: Union[str, List[str]],
    subject: str,
    bcc_recipients: Optional[List[str]] = None,
    body_text: Optional[str] = None,
    body_html: Optional[str] = None,
    attachment_bytes: Optional[Union[bytes, io.BytesIO]] = None,
    attachment_filename: Optional[str] = None,
    html_template_path: Optional[str] = None,
    html_template_str: Optional[str] = None,
    template_context: Optional[Dict[str, Any]] = None,
    sender: Optional[str] = None,
    aws_region: str = "ap-south-1",
    sender_name: Optional[str] = None,
    reply_to: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    check_suppression: bool = True,
) -> Dict:
    """A mail with an optional file and an optional HTML template, sent
    through whichever provider is configured (see `transport`). Returns
    `{"MessageId", "Provider"}`. `aws_region` is kept for old callers: the
    region is APP_SES_REGION's. `idempotency_key` and `check_suppression`
    are `transport.Mail`'s."""
    if isinstance(recipients, str):
        recipients = [recipients]
    if (html_template_path or html_template_str) and template_context:
        try:
            body_html = render_template(template_path=html_template_path, template_str=html_template_str, context=template_context)
        except Exception:
            logger.exception("Template rendering error")
    attachments = []
    if attachment_bytes:
        attachments.append(transport.Attachment(filename=attachment_filename or "attachment.pdf", content=transport.as_bytes(attachment_bytes)))
    mail = transport.Mail(
        to=list(recipients), bcc=list(bcc_recipients or []), subject=subject, text=body_text, html=body_html,
        from_name=sender_name, from_address=sender, reply_to=reply_to, attachments=attachments,
        idempotency_key=idempotency_key, check_suppression=check_suppression,
    )
    try:
        return transport.deliver(mail)
    except Exception:
        logger.exception("Error sending email")
        raise


def send_email_background(
    background_tasks: BackgroundTasks,
    recipients: Union[str, List[str]],
    subject: str,
    body_text: Optional[str] = None,
    body_html: Optional[str] = None,
    attachment_bytes: Optional[Union[bytes, io.BytesIO]] = None,
    attachment_filename: Optional[str] = None,
    html_template_path: Optional[str] = None,
    html_template_str: Optional[str] = None,
    template_context: Optional[Dict[str, Any]] = None,
    sender: Optional[str] = None,
):
    """
    Add email sending task to FastAPI's background tasks.

    :param background_tasks: FastAPI BackgroundTasks instance
    :param ... (same parameters as send_email_with_attachment)
    """
    background_tasks.add_task(
        send_email_with_attachment,
        recipients=recipients,
        subject=subject,
        body_text=body_text,
        body_html=body_html,
        attachment_bytes=attachment_bytes,
        attachment_filename=attachment_filename,
        html_template_path=html_template_path,
        html_template_str=html_template_str,
        template_context=template_context,
        sender=sender,
    )


def send_email(
    recipients: List[str],
    subject: str,
    text: str,
    html: str,
    attachments: Optional[List[Dict[str, Any]]] = None,
    sender: Optional[str] = None,
    sender_name: Optional[str] = None,
    aws_region: str = "ap-south-1",
    reply_to: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    check_suppression: bool = True,
) -> Dict:
    """A designed email: a text and an HTML body, files attached, and pictures
    inline by Content-ID (`{"filename", "content", "content_id"}`) so the
    guest's QR shows in the message rather than hanging off it. Sent through
    whichever provider is configured (see `transport`)."""
    files = [
        transport.Attachment(filename=a.get("filename") or "attachment", content=transport.as_bytes(a["content"]), content_id=a.get("content_id"))
        for a in attachments or []
    ]
    mail = transport.Mail(
        to=list(recipients), subject=subject, text=text, html=html, from_name=sender_name, from_address=sender, reply_to=reply_to,
        attachments=files, idempotency_key=idempotency_key, check_suppression=check_suppression,
    )
    return transport.deliver(mail)
