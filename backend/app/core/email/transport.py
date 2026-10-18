"""Where a mail goes once it is written: Resend or SES.

The two send functions in `email.py` describe a mail — who from, who to,
the subject, the text and HTML, the files — and hand it here. This picks
the provider and returns `{"MessageId", "Provider"}`, the id the provider
gave us, which the delivery hooks later match against.

`MAIL_PROVIDER=resend` sends through Resend first and falls back to SES only
when Resend definitely did not take the mail: it refused it (a 4xx: a bad
address, the day's quota, the rate) or could not be reached at all. When
Resend may have taken it -- a timeout after the request went, a 5xx -- SES
is not asked, since that would be the guest's second copy: `MailDeferred`
is raised, and the send is tried again later under the same idempotency
key, which Resend honours for a day. After a quota refusal Resend is left
alone until the next UTC day. `MAIL_PROVIDER=ses`, the default, sends
through SES alone.

One SES client serves the process (`_ses`), built once under a lock: a
client per mail meant a fresh TLS handshake to ap-south-1 each time -- some
three round trips from us-east-1, ~0.6 s -- and boto3's default session is
not safe to build clients from on many threads at once. A built client is.
Resend's HTTP client is shared the same way.

Errors: `MailThrottled` (slow down and try again), `MailDeferred` (try
again later; the mail may or may not have gone), anything else is the
provider's final word on this mail.
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
from botocore.config import Config
from botocore.exceptions import ClientError, ConnectionClosedError, ConnectTimeoutError, EndpointConnectionError, ReadTimeoutError

from app.config import settings

logger = logging.getLogger(__name__)

RESEND_URL = "https://api.resend.com/emails"


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
    #: The same key on a retry makes Resend send it once (it keeps keys for a day).
    idempotency_key: Optional[str] = None
    #: False when the caller already left out suppressed addresses (app.core.mail.suppression).
    check_suppression: bool = True


class ResendRefused(Exception):
    def __init__(self, status: int, name: str, message: str):
        super().__init__(f"Resend {status} {name}: {message}")
        self.status = status
        self.name = name


class MailDeferred(Exception):
    """Not sent now, and worth trying again later. The provider may have taken it."""


class MailThrottled(MailDeferred):
    """The provider's rate was exceeded: slow down, then try again."""


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
    global _resend_paused_until, _ses_client, _ses_key, _http_client
    with _lock:
        _resend_paused_until = None
    with _clients_lock:
        _ses_client, _ses_key = None, None
        if _http_client is not None:
            _http_client.close()
        _http_client = None


def deliver(mail: Mail) -> dict:
    """Send the mail and return the provider's id for it. Raises when it was
    not sent: `MailDeferred` when it may be tried again, anything else when
    every provider refused it."""
    if not mail.to:
        raise ValueError("A mail needs at least one recipient")
    if mail.check_suppression:
        from app.core.mail import suppression

        suppression.warn_sync([*mail.to, *mail.bcc], mail.subject)
    if _resend_available():
        try:
            return {"MessageId": _send_resend(mail), "Provider": "resend"}
        except ResendRefused as e:
            if e.status == 409 or e.status >= 500:
                # 5xx: it may have been taken. 409: the same key is still being
                # worked on. Either way SES now could be the guest's second copy.
                raise MailDeferred(f"Resend could not confirm the mail ({e}); it will be tried again") from e
            if e.name in ("daily_quota_exceeded", "monthly_quota_exceeded"):
                _pause_resend_until_tomorrow()
            logger.warning("Resend refused (%s); sending through SES", e)
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            # Never reached Resend, so nothing was taken.
            logger.warning("Resend unreachable (%s); sending through SES", e)
        except httpx.HTTPError as e:
            # Sent, perhaps taken, and no answer.
            raise MailDeferred(f"Resend did not answer ({e.__class__.__name__}); it will be tried again") from e
    return {"MessageId": _send_ses(mail), "Provider": "ses"}


# ─── Resend ──────────────────────────────────────────────────────────────

_clients_lock = threading.Lock()
_http_client: Optional[httpx.Client] = None


def _http() -> httpx.Client:
    global _http_client
    if _http_client is None:
        with _clients_lock:
            if _http_client is None:
                _http_client = httpx.Client(timeout=httpx.Timeout(20.0, connect=5.0))
    return _http_client


def _post_resend(body: dict, headers: dict):
    """One POST to Resend on the shared client. Tests stand in for it."""
    return _http().post(RESEND_URL, json=body, headers=headers)


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


def _send_resend(mail: Mail) -> Optional[str]:
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
    headers = {"Authorization": f"Bearer {settings.RESEND_API_KEY}"}
    if mail.idempotency_key:
        headers["Idempotency-Key"] = mail.idempotency_key[:256]
    r = _post_resend(body, headers)
    if r.status_code >= 300:
        try:
            err = r.json()
        except ValueError:
            err = {}
        raise ResendRefused(r.status_code, str(err.get("name") or ""), str(err.get("message") or r.text[:200]))
    message_id = (r.json() or {}).get("id")
    if not message_id:
        # Taken, with nothing to match its webhooks by. Sending it again
        # through SES would be a second copy, so it stands.
        logger.warning("Resend accepted a mail without an id")
    logger.info("Email sent through Resend: %s", message_id)
    return message_id


# ─── SES ─────────────────────────────────────────────────────────────────

_ses_client = None
_ses_key: Optional[tuple] = None

#: SES's answers that mean "not now" rather than "not this mail".
SES_THROTTLED = {"Throttling", "ThrottlingException", "TooManyRequestsException"}
SES_TRANSIENT = {"ServiceUnavailable", "ServiceUnavailableException", "InternalFailure", "RequestTimeout", "RequestTimeoutException"}


def _new_ses_client(region: str, key: str, secret: str):
    """A client on a session of its own (the default session is not safe to
    build clients from on several threads), with a connection pool as wide as
    the sends that may share it."""
    session = boto3.session.Session(aws_access_key_id=key, aws_secret_access_key=secret, region_name=region)
    return session.client(
        "ses",
        config=Config(
            region_name=region,
            retries={"max_attempts": 3, "mode": "standard"},
            connect_timeout=5,
            read_timeout=20,
            max_pool_connections=max(10, 2 * int(settings.MAIL_CONCURRENCY)),
        ),
    )


def _ses():
    """The process's SES client. A change of region or keys builds a new one."""
    global _ses_client, _ses_key
    key = (settings.SES_REGION or "ap-south-1", settings.SES_ACCESS_KEY, settings.SES_SECRET_KEY)
    client = _ses_client
    if client is not None and _ses_key == key:
        return client
    with _clients_lock:
        if _ses_client is None or _ses_key != key:
            _ses_client = _new_ses_client(*key)
            _ses_key = key
        return _ses_client


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
    try:
        response = _ses().send_raw_email(Source=from_header, Destinations=list(mail.to) + list(mail.bcc), RawMessage={"Data": msg.as_string()})
    except ClientError as e:
        code = (e.response.get("Error") or {}).get("Code") or ""
        status = (e.response.get("ResponseMetadata") or {}).get("HTTPStatusCode") or 0
        if code in SES_THROTTLED:
            raise MailThrottled(f"SES is throttling ({code}); it will be tried again") from e
        if code in SES_TRANSIENT or status >= 500:
            raise MailDeferred(f"SES could not take it just now ({code or status}); it will be tried again") from e
        raise
    except (EndpointConnectionError, ConnectTimeoutError, ReadTimeoutError, ConnectionClosedError) as e:
        raise MailDeferred(f"SES did not answer ({e.__class__.__name__}); it will be tried again") from e
    logger.info("Email sent through SES: %s", response.get("MessageId"))
    return response["MessageId"]


def as_bytes(content: Union[bytes, io.BytesIO]) -> bytes:
    return content.getvalue() if isinstance(content, io.BytesIO) else content
