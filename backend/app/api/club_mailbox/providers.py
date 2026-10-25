"""Where received mail comes from: one provider today (SES), more later.

A provider turns its own notification into a `Received` -- who the mail is
for (the envelope's recipients), its id, the provider's verdicts -- and,
asked, reads the raw MIME. Everything after that (inbound.py: which club,
spam, threading, keeping it) is the provider's no longer, so another
provider (Resend's inbound, say) is one more class here and one more
webhook route, and nothing else changes.

SES (receipt rule -> S3 action -> SNS topic): the notification says where
the mail was written (`receipt.action.bucketName`/`objectKey`, the key being
SES's message id), and gives SES's verdicts -- spam, virus, SPF, DKIM, DMARC
(AWS's "Contents of notifications for Amazon SES email receiving"). Only the
configured bucket is ever read (settings.MAILBOX_INBOUND_BUCKET), in the
receiving region, with the mailbox's own keys (or the SES ones).
"""
from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)

SES = "ses"
#: SES's message ids (and so the bucket's keys): letters, digits, a few marks.
_KEY = re.compile(r"^[A-Za-z0-9!_.*'()/-]{1,512}$")


class NotOurs(Exception):
    """A notification about a bucket or an action this mailbox doesn't read."""


class Unreadable(Exception):
    """The raw mail couldn't be read now (the bucket unreachable): try again."""


class TooLarge(Exception):
    """The raw mail is larger than settings.MAILBOX_INBOUND_MAX_MB."""


@dataclass
class Received:
    provider: str
    provider_message_id: str
    #: The envelope's recipients (RCPT TO), lower case: who it is for, whatever its headers say.
    recipients: list[str]
    spam: bool = False
    virus: bool = False
    #: The provider's verdicts, as kept on the message: {"spam": "PASS", ...}.
    verdicts: dict = field(default_factory=dict)
    #: Where the provider keeps the raw mail.
    bucket: Optional[str] = None
    key: Optional[str] = None


def _status(receipt: dict, name: str) -> Optional[str]:
    value = (receipt.get(name) or {}).get("status")
    return str(value).upper()[:24] if value else None


def ses_received(message: dict) -> Received:
    """A `Received` from SES's notification (the SNS envelope's Message,
    decoded). NotOurs when it isn't a received mail written to our bucket."""
    if not isinstance(message, dict) or message.get("notificationType") != "Received":
        raise NotOurs("not a received mail")
    receipt = message.get("receipt") or {}
    mail = message.get("mail") or {}
    action = receipt.get("action") or {}
    if action.get("type") != "S3":
        raise NotOurs("not an S3 action")
    bucket = str(action.get("bucketName") or "")
    if not settings.MAILBOX_INBOUND_BUCKET or bucket != settings.MAILBOX_INBOUND_BUCKET:
        raise NotOurs("another bucket")
    key = str(action.get("objectKey") or "")
    if not _KEY.match(key) or ".." in key:
        raise NotOurs("not a key we read")
    message_id = str(mail.get("messageId") or key.rsplit("/", 1)[-1])[:200]
    recipients = [str(r).strip().lower() for r in (receipt.get("recipients") or []) if isinstance(r, str) and "@" in r][:100]
    verdicts = {name: _status(receipt, f"{name}Verdict") for name in ("spam", "virus", "spf", "dkim", "dmarc")}
    if receipt.get("dmarcPolicy"):
        verdicts["dmarc_policy"] = str(receipt.get("dmarcPolicy"))[:16].lower()
    verdicts = {k: v for k, v in verdicts.items() if v}
    spam = verdicts.get("spam") == "FAIL" or (
        verdicts.get("dmarc") == "FAIL" and verdicts.get("dmarc_policy") in ("reject", "quarantine")
    )
    return Received(
        provider=SES, provider_message_id=message_id, recipients=recipients, spam=spam,
        virus=verdicts.get("virus") == "FAIL", verdicts=verdicts, bucket=bucket, key=key,
    )


# ─── Reading the raw mail from S3 ────────────────────────────────────────

_lock = threading.Lock()
_client = None
_client_key: Optional[tuple] = None


def _s3():
    """The process's S3 client for the inbound bucket, in the receiving region."""
    global _client, _client_key
    import boto3
    from botocore.config import Config

    key = (
        settings.MAILBOX_RECEIVING_REGION or "ap-south-1",
        settings.MAILBOX_AWS_ACCESS_KEY or settings.SES_ACCESS_KEY,
        settings.MAILBOX_AWS_SECRET_KEY or settings.SES_SECRET_KEY,
    )
    with _lock:
        if _client is None or _client_key != key:
            session = boto3.session.Session(aws_access_key_id=key[1], aws_secret_access_key=key[2], region_name=key[0])
            _client = session.client("s3", config=Config(retries={"max_attempts": 3, "mode": "standard"}, connect_timeout=5, read_timeout=30))
            _client_key = key
        return _client


def read(received: Received) -> bytes:
    """The raw MIME. Blocking (a worker thread's). TooLarge past the limit,
    Unreadable when the bucket can't be read now."""
    limit = int(settings.MAILBOX_INBOUND_MAX_MB or 40) * 1024 * 1024
    try:
        obj = _s3().get_object(Bucket=received.bucket, Key=received.key)
    except Exception as exc:  # noqa: BLE001
        raise Unreadable(type(exc).__name__) from None
    size = int(obj.get("ContentLength") or 0)
    if size > limit:
        obj["Body"].close()
        raise TooLarge(f"{size} bytes")
    data = obj["Body"].read(limit + 1)
    if len(data) > limit:
        raise TooLarge(f"over {limit} bytes")
    return data


def discard(received: Received) -> None:
    """Delete the raw mail once it is kept (or dropped). Never raises: the
    bucket's lifecycle rule deletes what this misses (OPERATIONS section 23)."""
    try:
        _s3().delete_object(Bucket=received.bucket, Key=received.key)
    except Exception as exc:  # noqa: BLE001
        logger.info("mailbox: the raw mail was left in the bucket (%s)", type(exc).__name__)


def reset_for_tests() -> None:
    global _client, _client_key
    with _lock:
        _client, _client_key = None, None
