"""WhatsApp: the templates Meta approved, and sending them through the Cloud API.

Off until the server's .env has the access token and the phone number id
(`configured`): with either missing nothing here is called, and a send on
the channel is recorded as skipped with the reason. Nothing but an approved
template is ever sent -- a club's own words cannot go by WhatsApp outside a
conversation the guest started, so the journey emails stay email.

The templates, as submitted to Meta (category UTILITY, language `en`). Each
builder below fills exactly these, in this order; a change to the wording at
Meta that adds, drops or reorders a value must change its builder too, or
Meta refuses every send with 132000.

- ticket_confirmed: header DOCUMENT (the ticket PDF). Body: "Hi {{1}}, you're
  registered for {{2}} on {{3}}. Your ticket ({{4}}) is attached. Show its
  QR code at the entry." -- first name, event, date and time, ticket id.
  URL button https://myotherapp.in/tickets/{{1}} -- the ticket id.
- event_reminder: body "Hi {{1}}, a reminder that {{2}} starts {{3}} at
  {{4}}. Keep your ticket ready at the entry." -- first name, event, date and
  time, the venue or "online". URL button .../tickets/{{1}}.
- certificate_ready: header DOCUMENT (the certificate PDF). Body "Hi {{1}},
  thank you for attending {{2}}. Your certificate from {{3}} is attached.
  Anyone can check it is genuine on myotherapp.in with the number {{4}}." --
  first name, event, club, certificate number. URL button
  https://myotherapp.in/verify/{{1}} -- the certificate number.
- payment_pending: body "Hi {{1}}, your registration for {{2}} is waiting
  for a payment of ₹{{3}}. Complete it to keep your spot." URL button
  https://myotherapp.in/events/{{1}}/register -- the event's slug.
- event_update: body "Hi {{1}}, {{2}} has an update about {{3}}: {{4}}. See
  the event page for details." -- first name, club, event, the update.

A document goes up to Meta first (POST /{phone number id}/media) and the
message names its media id: the file is never put at an address anyone
could guess or reuse, and Meta deletes it after 30 days.

What Meta answers, and what the send becomes (the delivery's status):

- `Undeliverable` -- a fact about the number: none on file, not a mobile
  number, WhatsApp says it cannot receive (131026), or the person stopped
  this business's marketing (131050). Skipped; with `permanent`, the
  registration's number is marked so later sends skip it until it is
  corrected -- for 131026 only the second time (`STRIKE_CODES`): a first
  can be an old WhatsApp or terms not yet accepted, which people fix.
- `Deferred` -- Meta is busy or down (rate limits 4, 17, 130429, 131048,
  131056, 80007; 131000, 131016; the account in maintenance, 131057; a
  5xx), or was never reached. Tried again with
  the backoff email uses, and given up on after MAIL_MAX_ATTEMPTS.
- `Refused` -- our side: the template (132xxx), the token, the account.
  Failed, with Meta's code in the reason; trying again would only fail
  again.
- `Unconfirmed` -- the message went and no answer came back. Failed, not
  tried again: there is no idempotency key on Meta's side, and a second try
  could be the guest's second copy.

The message id Meta gives back (wamid...) is the send's `provider_id`, which
the status webhook (app/api/hooks) matches its delivered/read/failed
notices against.

Every message sent from here goes from the PLATFORM's number, so it counts
against the club's monthly WhatsApp quota (app/api/message_quota). Every
template above is UTILITY at Meta, so none counts against the plan's cap on
promotional (MARKETING) WhatsApp; a MARKETING template sent from here must
say so in its `meter` ("category"). The delivery pipeline counts its own
sends in the transaction that records them (the answer's `Via` says which
number it went from); any other caller passes `meter` and the send is held
back past Free's limit and counted here.

A club on Enterprise may connect its own WhatsApp Business number
(app/api/club_whatsapp). Its sends name a `Sender` -- that number's id, its
access token, the language its templates were approved in -- and go through
the same code; their answer says `"Via": "club"`, and the club, which pays
Meta for them, is never counted or held back for them. Without a Sender,
everything here is the platform's number, from the server's .env.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote

import httpx

from app.config import settings
from app.core.email.transport import MailDeferred

logger = logging.getLogger(__name__)

GRAPH = "https://graph.facebook.com"
PROVIDER = "whatsapp"


# ─── Is it on ─────────────────────────────────────────────────────────────


def configured() -> bool:
    """Whether the server holds what a send needs: the token and the number to send from."""
    return bool((settings.WHATSAPP_ACCESS_TOKEN or "").strip() and (settings.WHATSAPP_PHONE_NUMBER_ID or "").strip())


# ─── Numbers ─────────────────────────────────────────────────────────────

_SEPARATORS = re.compile(r"[\s\-(). /]")
#: An Indian mobile number without its country code: ten digits from 6 to 9.
INDIAN_MOBILE = re.compile(r"[6-9]\d{9}")

NO_NUMBER = "no phone number"


def normalise(raw: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """The number in E.164 (+919876543210), or None and why not.

    Spaces, dashes, dots and brackets are ignored; 00 is read as +. With a
    country code it must be a plausible international number (7 to 15
    digits), and with +91 an Indian mobile. Without one it is taken as
    Indian: ten digits from 6 to 9, or those with a leading 0 or 91."""
    s = _SEPARATORS.sub("", (raw or "").strip())
    if not s:
        return None, NO_NUMBER
    if s.startswith("00"):
        s = "+" + s[2:]
    if s.startswith("+"):
        digits = s[1:]
        if not digits.isdigit():
            return None, "the number has more than digits in it"
        if digits.startswith("91"):
            if not INDIAN_MOBILE.fullmatch(digits[2:]):
                return None, "not an Indian mobile number (+91 and ten digits, the first 6 to 9)"
            return "+" + digits, None
        if not 7 <= len(digits) <= 15 or digits.startswith("0"):
            return None, "not a phone number with its country code"
        return "+" + digits, None
    if not s.isdigit():
        return None, "the number has more than digits in it"
    if len(s) == 11 and s.startswith("0"):
        s = s[1:]
    elif len(s) == 12 and s.startswith("91"):
        s = s[2:]
    if INDIAN_MOBILE.fullmatch(s):
        return "+91" + s, None
    return None, "not a mobile number (ten digits, the first 6 to 9, or the number with its country code)"


# ─── Whose number ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Sender:
    """A number other than the platform's: a club's own (app/api/club_whatsapp).
    The token is decrypted for the send and never kept or logged. With an
    `app_secret`, each call carries Meta's appsecret_proof, as a club's app
    may require."""

    phone_number_id: str
    token: str
    language: Optional[str] = None
    app_secret: Optional[str] = None
    #: Whose it is, for the caller: the club's account row.
    account_id: Optional[int] = None
    #: The Graph version its calls use, when not WHATSAPP_GRAPH_VERSION.
    version: Optional[str] = None

    def __repr__(self) -> str:  # never the token in a log line or a traceback
        return f"Sender(phone_number_id={self.phone_number_id!r}, account_id={self.account_id!r})"


def appsecret_proof(token: str, app_secret: Optional[str]) -> Optional[str]:
    """Meta's appsecret_proof: HMAC-SHA256 of the token, keyed by the app secret."""
    if not app_secret:
        return None
    import hashlib
    import hmac

    return hmac.new(app_secret.encode("utf-8"), token.encode("utf-8"), hashlib.sha256).hexdigest()


# ─── The templates ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class Template:
    """One approved template, filled: its name, the body's values in order,
    the URL button's suffix, and whether its header is a document."""

    name: str
    body: tuple[str, ...]
    button: Optional[str] = None
    document: bool = False


def _value(value, fallback: str, limit: int = 200) -> str:
    """A body value as Meta takes one: no new lines or tabs, no runs of
    spaces (132018), not empty (132000), not overlong."""
    text = " ".join(str(value if value is not None else "").split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text or fallback


def _suffix(value) -> str:
    """The URL button's dynamic part, safe in a path."""
    return quote(str(value or "").strip(), safe="-_.~")


def first_name(full_name: Optional[str]) -> str:
    return _value((full_name or "").strip().split(" ")[0] if full_name else "", "there", 60)


def ticket_confirmed(*, name: str, event: str, when: str, ticket_id: str) -> Template:
    return Template(
        "ticket_confirmed",
        (first_name(name), _value(event, "your event"), _value(when, "the day"), _value(ticket_id, "-", 60)),
        button=_suffix(ticket_id),
        document=True,
    )


def event_reminder(*, name: str, event: str, when: str, venue: Optional[str], ticket_id: str) -> Template:
    return Template(
        "event_reminder",
        (first_name(name), _value(event, "your event"), _value(when, "soon"), _value(venue, "online")),
        button=_suffix(ticket_id),
    )


def certificate_ready(*, name: str, event: str, club: str, number: str) -> Template:
    return Template(
        "certificate_ready",
        (first_name(name), _value(event, "the event"), _value(club, "the organisers"), _value(number, "-", 60)),
        button=_suffix(number),
        document=True,
    )


def payment_pending(*, name: str, event: str, amount: str, slug: str) -> Template:
    return Template(
        "payment_pending",
        (first_name(name), _value(event, "your event"), _value(amount, "0")),
        button=_suffix(slug),
    )


def event_update(*, name: str, club: str, event: str, update: str) -> Template:
    return Template(
        "event_update",
        (first_name(name), _value(club, "The organisers"), _value(event, "your event"), _value(update, "a change", 500)),
    )


def rupees_text(amount) -> str:
    """₹ amounts as Indians write them, without the sign the template has: 1,499 or 1,499.50."""
    from app.core.money import rupees

    value = rupees(amount)
    whole, paise = divmod(int(value * 100), 100)
    digits = str(whole)
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        digits = ",".join(groups + [tail])
    return digits if paise == 0 else f"{digits}.{paise:02d}"


def payload(
    to: str, template: Template, *, media_id: Optional[str] = None, filename: Optional[str] = None, language: Optional[str] = None,
) -> dict:
    """The body of POST /{phone number id}/messages for one template. The
    language is the platform's (WHATSAPP_TEMPLATE_LANGUAGE) unless a club's
    number had its own approved in another."""
    components: list[dict] = []
    if template.document:
        if not media_id:
            raise ValueError(f"{template.name} carries a document in its header, and none was uploaded")
        document = {"id": media_id}
        if filename:
            document["filename"] = filename
        components.append({"type": "header", "parameters": [{"type": "document", "document": document}]})
    components.append({"type": "body", "parameters": [{"type": "text", "text": text} for text in template.body]})
    if template.button is not None:
        components.append(
            {"type": "button", "sub_type": "url", "index": "0", "parameters": [{"type": "text", "text": template.button}]}
        )
    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to.lstrip("+"),
        "type": "template",
        "template": {
            "name": template.name,
            "language": {"code": language or settings.WHATSAPP_TEMPLATE_LANGUAGE or "en"},
            "components": components,
        },
    }


# ─── What Meta says ──────────────────────────────────────────────────────


class WhatsAppError(Exception):
    """Not sent; `reason` is in words a desk can act on, `code` is Meta's."""

    def __init__(self, reason: str, code: Optional[str] = None):
        super().__init__(reason)
        self.reason = reason[:400]
        self.code = code


class Undeliverable(WhatsAppError):
    """A fact about the number: the send is skipped. `permanent` marks the
    registration's number so later sends skip it until it is corrected."""

    def __init__(self, reason: str, code: Optional[str] = None, permanent: bool = False):
        super().__init__(reason, code)
        self.permanent = permanent


class Refused(WhatsAppError):
    """Our side -- the template, the token, the account. Failed, not retried."""


class Unconfirmed(WhatsAppError):
    """Sent, and no answer: it may have gone. Failed, never retried."""


class Deferred(MailDeferred):
    """Meta busy, rate-limiting or unreachable: tried again later."""

    def __init__(self, reason: str, code: Optional[str] = None):
        super().__init__(reason)
        self.code = code


#: Codes about the recipient's number. True: the number itself is dead to
#: WhatsApp (for STRIKE_CODES, once it has happened twice).
UNDELIVERABLE = {
    "131026": ("WhatsApp says this number cannot receive messages", True),
    "131021": ("The guest's number is the business's own", True),
    "131030": ("The number is not on the WhatsApp account's test list (the account is not live yet)", False),
    "131050": ("They chose to stop marketing messages from this business", False),
}
#: Codes that mark a number dead only the second time they are met: the
#: first is noted on the registration (FIRST_STRIKE) and the number kept.
STRIKE_CODES = frozenset({"131026"})
FIRST_STRIKE = "WhatsApp couldn't reach this number once"


def first_strike(code: Optional[str]) -> str:
    """What a registration says after a STRIKE_CODES failure that is not yet a second."""
    return f"{FIRST_STRIKE} ({code}); if it happens again, WhatsApp sends to it stop until the number is corrected"
#: Codes that mean "not now".
DEFERRED = {
    "1": "WhatsApp had an unknown problem",
    "2": "WhatsApp's service was unavailable",
    "4": "Too many calls to WhatsApp just now",
    "17": "Too many calls to WhatsApp just now",
    "80007": "The WhatsApp account's rate limit was reached",
    "130429": "WhatsApp's sending rate limit was reached",
    "131000": "WhatsApp could not send it just now",
    "131016": "WhatsApp's service was overloaded",
    "131048": "WhatsApp is limiting this account for its spam rate",
    "131056": "Too many messages to this number just now",
    "131057": "The WhatsApp account is in maintenance mode",
}
#: Codes about our side, in words.
REFUSED = {
    "0": "WhatsApp did not accept the access token",
    "3": "The WhatsApp app is not allowed to send messages",
    "10": "The access token is missing a WhatsApp permission",
    "100": "WhatsApp found a value it does not accept",
    "102": "The WhatsApp access token's session is no longer valid",
    "190": "The WhatsApp access token has expired or was revoked — set a new APP_WHATSAPP_ACCESS_TOKEN",
    "200": "The access token is missing a WhatsApp permission",
    "131008": "A value the template needs was missing",
    "131009": "A value the template was given is not valid",
    "131031": "The WhatsApp business account is locked",
    "131042": "The WhatsApp account has a payment problem",
    "131047": "Outside the 24-hour window — only an approved template can reach them now",
    "131064": "The WhatsApp account is limited for templates in the wrong category",
    "131051": "That kind of message is not supported",
    "131052": "WhatsApp could not fetch the attachment",
    "131053": "WhatsApp could not take the attachment",
    "132000": "The template needs a different number of values than were sent",
    "132001": "No approved template by that name in this language",
    "132005": "The text is too long for the template",
    "132007": "The template was refused for its content",
    "132012": "A value does not fit the template's format",
    "132015": "The template is paused for poor quality",
    "132016": "The template has been disabled",
    "132018": "There's a problem with the template's values (new lines, tabs or too many spaces in one)",
    "133010": "The sending number is not registered with WhatsApp",
    "368": "The business's WhatsApp account is restricted for a policy violation",
}


def refusal(status: int, body) -> Exception:
    """Meta's error answer as one of the errors above."""
    error = (body or {}).get("error") if isinstance(body, dict) else None
    error = error if isinstance(error, dict) else {}
    code = None if error.get("code") is None else str(error.get("code"))
    detail = ((error.get("error_data") or {}).get("details") if isinstance(error.get("error_data"), dict) else None) or error.get("message") or ""
    detail = str(detail).strip()

    def worded(words: str) -> str:
        return f"{words} ({code})" if code else words

    if code in UNDELIVERABLE:
        words, permanent = UNDELIVERABLE[code]
        return Undeliverable(worded(words), code, permanent=permanent)
    if code in DEFERRED or status == 429 or status >= 500:
        return Deferred(worded(DEFERRED.get(code) or f"WhatsApp answered {status}") + "; it will be tried again", code)
    if code == "100" and "appsecret_proof" in detail.lower():
        # The app secret isn't the token's app's: the keys, not a value in the message.
        return Refused(worded("The app secret doesn't belong to the access token's app (appsecret_proof)"), code)
    if code in REFUSED:
        return Refused(worded(REFUSED[code]), code)
    if detail:
        return Refused(worded(f"WhatsApp refused it: {detail}"), code)
    return Refused(f"WhatsApp refused it ({code or status})", code)


# ─── Sending ─────────────────────────────────────────────────────────────


def graph_version() -> str:
    return settings.WHATSAPP_GRAPH_VERSION.strip() or "v23.0"


def _base(sender: Optional[Sender] = None) -> str:
    number = sender.phone_number_id if sender is not None else settings.WHATSAPP_PHONE_NUMBER_ID.strip()
    return f"{GRAPH}/{(sender.version if sender is not None else None) or graph_version()}/{number}"


def _headers(sender: Optional[Sender] = None) -> dict:
    token = sender.token if sender is not None else settings.WHATSAPP_ACCESS_TOKEN.strip()
    return {"Authorization": f"Bearer {token}"}


def _params(sender: Optional[Sender] = None) -> Optional[dict]:
    proof = appsecret_proof(sender.token, sender.app_secret) if sender is not None else None
    return {"appsecret_proof": proof} if proof else None


def _client() -> httpx.AsyncClient:
    """The client for one send: the upload and the message share it. Tests stand in for it."""
    return httpx.AsyncClient(timeout=httpx.Timeout(float(settings.WHATSAPP_TIMEOUT_SECONDS or 15.0), connect=5.0))


def _json(r: httpx.Response):
    try:
        return r.json()
    except ValueError:
        return {}


async def _upload(client: httpx.AsyncClient, content: bytes, filename: str, sender: Optional[Sender] = None) -> str:
    """The document, up to Meta; its media id. Nothing has reached the guest
    yet, so any trouble here may be tried again."""
    try:
        r = await client.post(
            f"{_base(sender)}/media",
            headers=_headers(sender),
            params=_params(sender),
            data={"messaging_product": "whatsapp", "type": "application/pdf"},
            files={"file": (filename, content, "application/pdf")},
        )
    except httpx.HTTPError as e:
        raise Deferred(f"The attachment could not be uploaded to WhatsApp ({e.__class__.__name__}); it will be tried again") from e
    if r.status_code >= 300:
        raise refusal(r.status_code, _json(r))
    media_id = str((_json(r) or {}).get("id") or "")
    if not media_id:
        raise Deferred("WhatsApp took the attachment without an id; it will be tried again")
    return media_id


#: The number a send here goes from (app/api/message_quota counts these).
VIA = "platform"


async def _held_back(meter: dict) -> Optional[str]:
    from app.api.message_quota import service as quota

    async with quota.session_scope() as s:
        _, reason = await quota.check(
            s, meter.get("club_id"), quota.WHATSAPP, meter.get("kind") or "update", category=meter.get("category"),
        )
        await s.commit()
    return reason


async def _count(meter: dict) -> None:
    from app.api.message_quota import service as quota

    try:
        await quota.record_message(
            meter.get("club_id"), quota.WHATSAPP, meter.get("kind") or "update", ref=meter.get("ref"),
            category=meter.get("category"),
        )
    except Exception:  # noqa: BLE001 - sent is sent; a count that cannot be written is logged
        logger.warning("a WhatsApp %s for club %s could not be counted", meter.get("kind"), meter.get("club_id"), exc_info=True)


#: The number a club's own sends go from: never counted against its quota.
VIA_CLUB = "club"


async def send(
    number: Optional[str], template: Template, *, document: Optional[tuple[bytes, str]] = None, meter: Optional[dict] = None,
    sender: Optional[Sender] = None,
) -> dict:
    """One template to one number: `{"MessageId", "Provider", "Address",
    "Via"}`, the id Meta gave the message, the number it went to and the
    number it went from ("platform", or "club" from `sender`'s, a club's
    own). Raises one of the errors above when it was not sent.

    `meter` ({"club_id", "kind", "ref", "category"}) is for a send made
    outside the delivery pipeline: past a Free club's monthly WhatsApp, or a
    promotional one (category "MARKETING") past the plan's promotional cap,
    it is held back (Undeliverable, with the reason), and once Meta takes it,
    it is counted.
    The pipeline leaves it out: it checks and counts in its own transactions.
    A send from a club's own number is neither held back nor counted."""
    if sender is None and not configured():
        raise Undeliverable("no WhatsApp provider is configured — nothing was sent")
    to, why = normalise(number)
    if to is None:
        raise Undeliverable(f"no WhatsApp number: {why}")
    if template.document and document is None:
        raise ValueError(f"{template.name} needs its document")
    if meter is not None and sender is None:
        reason = await _held_back(meter)
        if reason:
            raise Undeliverable(reason)
    async with _client() as client:
        media_id = filename = None
        if template.document:
            content, filename = document
            media_id = await _upload(client, content, filename, sender)
        body = payload(to, template, media_id=media_id, filename=filename, language=sender.language if sender is not None else None)
        try:
            r = await client.post(f"{_base(sender)}/messages", headers=_headers(sender), params=_params(sender), json=body)
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            # Never reached Meta, so nothing was sent.
            raise Deferred(f"WhatsApp could not be reached ({e.__class__.__name__}); it will be tried again") from e
        except httpx.HTTPError as e:
            raise Unconfirmed(
                f"WhatsApp did not answer ({e.__class__.__name__}) — it may have gone, so it is not tried again on its own"
            ) from e
    if r.status_code >= 300:
        err = refusal(r.status_code, _json(r))
        if isinstance(err, Refused):
            logger.warning("WhatsApp refused %s: %s", template.name, err.reason)
        raise err
    messages = (_json(r) or {}).get("messages") or [{}]
    message_id = (messages[0] or {}).get("id") if isinstance(messages, list) else None
    if not message_id:
        # Taken, with nothing to match its statuses by. Sending again could be a second copy, so it stands.
        logger.warning("WhatsApp accepted %s without a message id", template.name)
    if sender is not None:
        return {"MessageId": message_id, "Provider": PROVIDER, "Address": to, "Via": VIA_CLUB}
    if meter is not None:
        await _count(meter)
    return {"MessageId": message_id, "Provider": PROVIDER, "Address": to, "Via": VIA}
