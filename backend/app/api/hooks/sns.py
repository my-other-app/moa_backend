"""Amazon SNS envelopes, verified on their signature.

SES publishes to an SNS topic; the topic calls us over HTTPS. Every call is
signed by Amazon with a certificate it also hosts, and only a call whose
signature verifies against a certificate from an sns.*.amazonaws.com host is
believed. The first call is a subscription to confirm, by fetching the URL
it carries, again only on an Amazon host.
"""
import base64
import json
import re
from typing import Callable, Optional
from urllib.parse import urlparse

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

AMAZON_SNS_HOST = re.compile(r"^sns\.[a-z0-9-]+\.amazonaws\.com(\.cn)?$")
AMAZON_HOST = re.compile(r"^[a-z0-9.-]+\.amazonaws\.com(\.cn)?$")

NOTIFICATION_FIELDS = ("Message", "MessageId", "Subject", "Timestamp", "TopicArn", "Type")
SUBSCRIPTION_FIELDS = ("Message", "MessageId", "SubscribeURL", "Timestamp", "Token", "TopicArn", "Type")

_certs: dict = {}


def fetch_text(url: str) -> str:
    return httpx.get(url, timeout=10, follow_redirects=False).text


# Tests replace this; production fetches from Amazon.
fetch: Callable[[str], str] = fetch_text


def parse(raw: bytes) -> Optional[dict]:
    try:
        msg = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    return msg if isinstance(msg, dict) and msg.get("Type") else None


def _amazon_url(url: Optional[str], pattern=AMAZON_HOST) -> bool:
    if not url:
        return False
    u = urlparse(url)
    return u.scheme == "https" and bool(pattern.match(u.hostname or ""))


def canonical(msg: dict) -> Optional[str]:
    fields = SUBSCRIPTION_FIELDS if msg.get("Type") in ("SubscriptionConfirmation", "UnsubscribeConfirmation") else NOTIFICATION_FIELDS
    lines = []
    for f in fields:
        if f in msg and msg[f] is not None:
            lines.append(f)
            lines.append(str(msg[f]))
    return "\n".join(lines) + "\n" if lines else None


def _certificate(url: str):
    if url not in _certs:
        _certs[url] = x509.load_pem_x509_certificate(fetch(url).encode("utf-8"))
        if len(_certs) > 16:
            _certs.pop(next(iter(_certs)))
    return _certs[url]


def verify(msg: dict) -> bool:
    """True only for a message Amazon signed, with a certificate Amazon hosts."""
    cert_url = msg.get("SigningCertURL") or msg.get("SigningCertUrl")
    if not _amazon_url(cert_url, AMAZON_SNS_HOST):
        return False
    text = canonical(msg)
    if text is None or not msg.get("Signature"):
        return False
    try:
        signature = base64.b64decode(msg["Signature"])
        cert = _certificate(cert_url)
        algorithm = hashes.SHA256() if str(msg.get("SignatureVersion")) == "2" else hashes.SHA1()
        cert.public_key().verify(signature, text.encode("utf-8"), padding.PKCS1v15(), algorithm)
        return True
    except Exception:
        return False


def confirm(msg: dict) -> bool:
    """Subscribe by fetching the URL Amazon gave us, on an Amazon host only."""
    url = msg.get("SubscribeURL")
    if not _amazon_url(url):
        return False
    fetch(url)
    return True
