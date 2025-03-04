import os
import boto3
from botocore.exceptions import BotoCoreError, ClientError
from typing import List, Dict, Optional
from jinja2 import Template
from fastapi import BackgroundTasks

from app.config import settings

# Initialize the SES client
ses = boto3.client(
    "ses",
    region_name="ap-south-1",
    aws_access_key_id=settings.SES_ACCESS_KEY,
    aws_secret_access_key=settings.SES_SECRET_KEY,
)


def render_template_from_file(template_path: str, context: Dict[str, str]) -> str:
    """
    Reads an HTML template file synchronously and renders it using Jinja2.
    """
    template_path = os.path.join("mail_templates/", template_path)
    if not os.path.exists(template_path):
        raise FileNotFoundError(f"Template file not found: {template_path}")

    with open(template_path, "r", encoding="utf-8") as file:
        template_str = file.read()

    template = Template(template_str)
    return template.render(context)


def _send_email(
    recipients: List[str],
    subject: str,
    template_path: Optional[str] = None,
    context: Optional[Dict[str, str]] = None,
    body_text: Optional[str] = None,
    sender: Optional[str] = None,
) -> Dict:
    print("Sending email to", recipients)
    try:
        sender = sender or settings.SES_DEFAULT_SENDER
        body_html = (
            render_template_from_file(template_path, context)
            if template_path and context
            else None
        )

        # Ensure at least one body part is present
        message_body = {}
        if body_text:
            message_body["Text"] = {"Data": body_text}
        if body_html:
            message_body["Html"] = {"Data": body_html}

        if not message_body:  # If neither Text nor Html is provided, raise an error
            raise ValueError(
                "Either body_text or a valid template (template_path + context) is required."
            )
        print("FROM", sender)
        print("TO", recipients)
        response = ses.send_email(
            Source=sender,
            Destination={"ToAddresses": recipients},
            Message={
                "Subject": {"Data": subject},
                "Body": message_body,
            },
        )
        return response
    except (BotoCoreError, ClientError, ValueError) as e:
        print("SES Error:", e)  # Debugging
        return {"error": str(e)}


def send_email_background(
    background_tasks: BackgroundTasks,
    recipients: List[str],
    subject: str,
    template_path: Optional[str] = None,
    context: Optional[Dict[str, str]] = None,
    body_text: Optional[str] = None,
    sender: Optional[str] = None,
):
    """
    Adds the email sending task to FastAPI's background tasks.
    """
    print("send email background")
    background_tasks.add_task(
        _send_email, recipients, subject, template_path, context, body_text, sender
    )
