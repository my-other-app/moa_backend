"""Where a mail goes once it is written: Resend or SES.

The two send functions in `email.py` describe a mail — who from, who to,
the subject, the text and HTML, the files — and hand it here. This picks
the provider and returns `{"MessageId", "Provider"}`, the id the provider
gave us, which the delivery hooks later match against.

`MAIL_PROVIDER=resend` sends through Resend first and falls back to SES on
any refusal: Resend's free plan stops at 100 mails a day, and a ticket
must never be lost to a quota. After a quota refusal Resend is left alone
until the next UTC day, so a busy evening does not ask it a thousand times.
`MAIL_PROVIDER=ses`, the default, sends through SES alone as before.
"""
from __future__ import annotations

import base64
import io
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.mime.application import MIMEApplication
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, parseaddr
from typing import Optional, Union

import boto3
import httpx

from app.config import settings

logger = logging.getLogger(__name__)

RESEND_URL = "https://api.resend.com/emails"
SES_REGION = "ap-south-1"


@dataclass
class Attachment:
    filename: str
    content: bytes
    #: Set for a picture shown inside the HTML by `cid:`; unset for a file attached to the mail.
    content_id: Optional[str] = None


@dataclass
class Mail:
    to: list[str]
    subject: str
    text: Optional[str] = None
    html: Optional[str] = None
    from_name: Optional[str] = None
    #: An explicit sender address; when unset, each provider uses its own verified one.
    from_address: Optional[str] = None
    bcc: list[str] = field(default_factory=list)
    reply_to: Optional[str] = None
    attachments: list[Attachment] = field(default_factory=list)


class ResendRefused(Exception):
    def __init__(self, status: int, name: str, message: str):
        super().__init__(f"Resend {status} {name}: {message}")
        self.status = status
        self.name = name


# ─── Which provider ──────────────────────────────────────────────────────

_lock = threading.Lock()
_resend_paused_until: Optional[datetime] = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _resend_available() -> bool:
    if (settings.MAIL_PROVIDER or "ses").lower() != "resend" or not settings.RESEND_API_KEY:
        return False
    with _lock:
        return _resend_paused_until is None or _now() >= _resend_paused_until


def _pause_resend_until_tomorrow() -> None:
    global _resend_paused_until
    tomorrow = (_now() + timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0)
    with _lock:
        _resend_paused_until = tomorrow
    logger.warning("Resend quota reached; sending through SES until %s", tomorrow.isoformat())


def reset_for_tests() -> None:
    global _resend_paused_until
    with _lock:
        _resend_paused_until = None


def deliver(mail: Mail) -> dict:
    """Send the mail and return the provider's id for it. Raises only when every provider refused."""
    if not mail.to:
        raise ValueError("A mail needs at least one recipient")
    if _resend_available():
        try:
            return {"MessageId": _send_resend(mail), "Provider": "resend"}
        except ResendRefused as e:
            if e.name in ("daily_quota_exceeded", "monthly_quota_exceeded"):
                _pause_resend_until_tomorrow()
            logger.warning("Resend refused (%s); sending through SES", e)
        except httpx.HTTPError as e:
            logger.warning("Resend unreachable (%s); sending through SES", e)
    return {"MessageId": _send_ses(mail), "Provider": "ses"}


# ─── Resend ──────────────────────────────────────────────────────────────


def _resend_from(mail: Mail) -> tuple[str, Optional[str]]:
    """Resend sends only from its verified domain. A sender on another domain
    becomes the Reply-To, so answers still reach whoever wrote."""
    domain = (settings.MAIL_FROM_ADDRESS.split("@", 1)[-1] or "").lower()
    name = mail.from_name or settings.MAIL_FROM_NAME
    wanted = (mail.from_address or "").strip()
    on_domain = wanted and (wanted.lower().endswith("@" + domain) or wanted.lower().endswith("." + domain))
    if on_domain:
        return formataddr((name, wanted)), mail.reply_to
    return formataddr((name, settings.MAIL_FROM_ADDRESS)), mail.reply_to or (wanted or None)


def _send_resend(mail: Mail) -> str:
    sender, reply_to = _resend_from(mail)
    body: dict = {"from": sender, "to": mail.to, "subject": mail.subject}
    if mail.html:
        body["html"] = mail.html
    if mail.text:
        body["text"] = mail.text
    if mail.bcc:
        body["bcc"] = mail.bcc
    if reply_to:
        body["reply_to"] = reply_to
    if mail.attachments:
        body["attachments"] = [
            {"filename": a.filename, "content": base64.b64encode(a.content).decode("ascii"), **({"content_id": a.content_id} if a.content_id else {})}
            for a in mail.attachments
        ]
    r = httpx.post(RESEND_URL, json=body, headers={"Authorization": f"Bearer {settings.RESEND_API_KEY}"}, timeout=20.0)
    if r.status_code >= 300:
        try:
            err = r.json()
        except ValueError:
            err = {}
        raise ResendRefused(r.status_code, str(err.get("name") or ""), str(err.get("message") or r.text[:200]))
    message_id = (r.json() or {}).get("id")
    if not message_id:
        raise ResendRefused(r.status_code, "no_id", "Resend accepted the mail without an id")
    logger.info("Email sent through Resend: %s", message_id)
    return message_id


# ─── SES ─────────────────────────────────────────────────────────────────


def to_mime(mail: Mail, from_header: str) -> MIMEMultipart:
    """multipart/mixed > related > alternative: what mail clients agree on,
    with pictures inline by Content-ID and files attached."""
    related = MIMEMultipart("related")
    alternative = MIMEMultipart("alternative")
    alternative.attach(MIMEText(mail.text or "", "plain", "utf-8"))
    if mail.html:
        alternative.attach(MIMEText(mail.html, "html", "utf-8"))
    related.attach(alternative)
    msg = MIMEMultipart("mixed")
    msg["Subject"] = mail.subject
    msg["From"] = from_header
    msg["To"] = ", ".join(mail.to)
    if mail.reply_to:
        msg["Reply-To"] = mail.reply_to
    msg.attach(related)
    for a in mail.attachments:
        ext = a.filename.rsplit(".", 1)[-1] if "." in a.filename else ""
        if a.content_id:
            part = MIMEImage(a.content, _subtype=ext or "png")
            part.add_header("Content-ID", f"<{a.content_id}>")
            part.add_header("Content-Disposition", "inline", filename=a.filename)
            related.attach(part)
        else:
            part = MIMEApplication(a.content, _subtype=ext or "octet-stream")
            part.add_header("Content-Disposition", "attachment", filename=a.filename)
            msg.attach(part)
    return msg


def _send_ses(mail: Mail) -> str:
    # SES sends from the identity verified there; the requested sender, if any, still wins.
    address = mail.from_address or settings.SES_DEFAULT_SENDER
    from_header = formataddr((mail.from_name or settings.MAIL_FROM_NAME, parseaddr(address)[1] or address))
    msg = to_mime(mail, from_header)
    client = boto3.client("ses", region_name=SES_REGION, aws_access_key_id=settings.SES_ACCESS_KEY, aws_secret_access_key=settings.SES_SECRET_KEY)
    response = client.send_raw_email(Source=from_header, Destinations=list(mail.to) + list(mail.bcc), RawMessage={"Data": msg.as_string()})
    logger.info("Email sent through SES: %s", response.get("MessageId"))
    return response["MessageId"]


def as_bytes(content: Union[bytes, io.BytesIO]) -> bytes:
    return content.getvalue() if isinstance(content, io.BytesIO) else content
