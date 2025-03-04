import os
import io
import boto3
from botocore.exceptions import BotoCoreError, ClientError
from typing import Any, List, Dict, Optional, Union
from jinja2 import Template
from fastapi import BackgroundTasks
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication

from app.config import settings

# Initialize the SES client
ses = boto3.client(
    "ses",
    region_name="ap-south-1",
    aws_access_key_id=settings.SES_ACCESS_KEY,
    aws_secret_access_key=settings.SES_SECRET_KEY,
)


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

    template = Template(template_str)
    return template.render(context or {})


def send_email_with_attachment(
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
    aws_region: str = "ap-south-1",
) -> Dict:
    """
    Send an email with optional attachment and HTML template.

    :param recipients: Single email or list of email addresses
    :param subject: Email subject
    :param body_text: Plain text body of the email
    :param body_html: HTML body of the email
    :param attachment_bytes: Bytes or BytesIO object for attachment
    :param attachment_filename: Filename for the attachment
    :param html_template_path: Path to HTML template file
    :param html_template_str: Direct HTML template string
    :param template_context: Context for rendering HTML template
    :param sender: Sender email (defaults to settings)
    :param aws_region: AWS region for SES
    :return: SES send_raw_email response
    """
    # Normalize recipients to a list
    if isinstance(recipients, str):
        recipients = [recipients]

    # Prepare the message
    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = sender or settings.SES_DEFAULT_SENDER
    msg["To"] = ", ".join(recipients)

    # Render HTML template if provided
    if (html_template_path or html_template_str) and template_context:
        try:
            body_html = render_template(
                template_path=html_template_path,
                template_str=html_template_str,
                context=template_context,
            )
        except Exception as e:
            print(f"Template rendering error: {e}")

    # Attach plain text body
    if body_text:
        msg.attach(MIMEText(body_text, "plain"))

    # Attach HTML body
    if body_html:
        msg.attach(MIMEText(body_html, "html"))

    # Attach file if provided
    if attachment_bytes:
        # Convert to bytes if it's a BytesIO object
        if isinstance(attachment_bytes, io.BytesIO):
            attachment_bytes = attachment_bytes.getvalue()

        # Use a default filename if not provided
        attachment_filename = attachment_filename or "attachment.pdf"

        attachment_part = MIMEApplication(
            attachment_bytes, _subtype=attachment_filename.split(".")[-1]
        )
        attachment_part.add_header(
            "Content-Disposition", "attachment", filename=attachment_filename
        )
        msg.attach(attachment_part)

    # Create SES client
    ses_client = boto3.client(
        "ses",
        region_name=aws_region,
        aws_access_key_id=settings.SES_ACCESS_KEY,
        aws_secret_access_key=settings.SES_SECRET_KEY,
    )

    # Send the email
    try:
        response = ses_client.send_raw_email(
            Source=msg["From"],
            Destinations=recipients,
            RawMessage={"Data": msg.as_string()},
        )
        print(f"Email sent successfully! Message ID: {response['MessageId']}")
        return response
    except Exception as e:
        print(f"Error sending email: {e}")
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
