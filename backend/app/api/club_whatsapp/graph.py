"""Meta's Graph API, on a club's own keys.

Every call names the club's token (and, when the club saved its app secret,
carries Meta's appsecret_proof, which an app may require -- and which proves
the secret belongs to the token's app). Nothing here keeps a token, logs one,
or logs what a message says: an error is Meta's code and its words about the
call, never the body we sent.

`_client` is the one door to the network; the suite stands in for it
(tests/conftest.py makes every call fail as an unreachable host would, and
tests/test_club_whatsapp.py answers as Meta would).

What a refusal means to the club is `explain`: one error code and one
sentence per thing it can fix (the token, the number, a permission, the app
secret), or META_UNREACHABLE when Meta was busy or could not be reached.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlsplit

import httpx

from app.config import settings
from app.core import whatsapp

GRAPH = whatsapp.GRAPH
#: The number's fields the connect check and the quality refresh read. The
#: messaging limit is the business portfolio's now (Meta, October 2025):
#: `whatsapp_business_manager_messaging_limit`, from v24.0; the old
#: `messaging_limit_tier` is deprecated.
PHONE_FIELDS = "display_phone_number,verified_name,quality_rating,whatsapp_business_manager_messaging_limit,code_verification_status,name_status,status"
#: Without the beta `name_status`, for an app Meta doesn't show it to.
PHONE_FIELDS_PLAIN = "display_phone_number,verified_name,quality_rating,whatsapp_business_manager_messaging_limit,code_verification_status,status"
#: The oldest Graph version the club's calls use: the one the messaging
#: limit field needs. The platform's own sends keep WHATSAPP_GRAPH_VERSION.
MIN_VERSION = (24, 0)
#: The permissions a club's token needs (Meta's permissions page).
SCOPES = ("whatsapp_business_management", "whatsapp_business_messaging")
TEMPLATE_FIELDS = "id,name,language,status,category,components,rejected_reason,quality_score"
#: Pages of templates read at most (100 a page).
TEMPLATE_PAGES = 10


def _client(timeout: Optional[float] = None) -> httpx.AsyncClient:
    """One call's client. Tests stand in for it."""
    seconds = float(timeout or settings.WHATSAPP_TIMEOUT_SECONDS or 15.0)
    return httpx.AsyncClient(timeout=httpx.Timeout(seconds, connect=5.0))


def version() -> str:
    """The Graph version of a club's calls: WHATSAPP_GRAPH_VERSION, or v24.0
    when that is older (production's .env may pin one)."""
    configured = whatsapp.graph_version()
    try:
        major, minor = (int(p) for p in configured.lstrip("v").split(".", 1))
    except ValueError:
        return "v{}.{}".format(*MIN_VERSION)
    return configured if (major, minor) >= MIN_VERSION else "v{}.{}".format(*MIN_VERSION)


def url(path: str) -> str:
    return f"{GRAPH}/{version()}/{path.lstrip('/')}"


@dataclass
class Keys:
    """What a call needs of a club's account, decrypted for the call alone."""

    token: str
    app_secret: Optional[str] = None
    waba_id: str = ""
    phone_number_id: str = ""
    app_id: Optional[str] = None

    def __repr__(self) -> str:  # never the token or the secret
        return f"Keys(waba_id={self.waba_id!r}, phone_number_id={self.phone_number_id!r})"


class GraphError(Exception):
    """Meta said no, or could not be asked. `unreachable` is true for what
    may go through later: no answer, a 5xx, a rate limit."""

    def __init__(self, message: str, *, status: int = 0, code: Optional[str] = None, subcode: Optional[str] = None,
                 title: Optional[str] = None, user_message: Optional[str] = None, unreachable: bool = False,
                 body: Optional[dict] = None):
        super().__init__(message)
        self.message = message[:300]
        self.status = status
        self.code = code
        self.subcode = subcode
        #: Meta's own words for a person: error_user_title, error_user_msg.
        self.title = title
        self.user_message = (user_message or None) and str(user_message)[:400]
        self.unreachable = unreachable
        #: Meta's own answer, for the send's error mapping (whatsapp.refusal).
        self.body = body or {}


#: Codes that mean "not now": Meta's own trouble, or a rate limit.
BUSY = {"1", "2", "4", "17", "32", "613", "80007", "130429", "131000", "131016", "131057"}
#: Codes that say the token no longer works, or lost a permission: the
#: club's keys, not the thing asked about.
TOKEN_CODES = frozenset({"0", "3", "10", "102", "190", "200"})


def is_token_problem(exc: "GraphError") -> bool:
    return exc.code in TOKEN_CODES or "appsecret_proof" in (exc.message or "").lower()


def _error(r: httpx.Response) -> GraphError:
    try:
        body = r.json()
    except ValueError:
        body = {}
    error = body.get("error") if isinstance(body, dict) else None
    error = error if isinstance(error, dict) else {}
    code = None if error.get("code") is None else str(error.get("code"))
    subcode = None if error.get("error_subcode") is None else str(error.get("error_subcode"))
    details = (error.get("error_data") or {}).get("details") if isinstance(error.get("error_data"), dict) else None
    message = " ".join(str(details or error.get("message") or f"Meta answered {r.status_code}").split())
    unreachable = r.status_code >= 500 or r.status_code == 429 or code in BUSY
    return GraphError(message, status=r.status_code, code=code, subcode=subcode, title=error.get("error_user_title"),
                      user_message=error.get("error_user_msg"), unreachable=unreachable, body=body if isinstance(body, dict) else {})


def _auth(keys: Keys, params: Optional[dict]) -> tuple[dict, dict]:
    headers = {"Authorization": f"Bearer {keys.token}"}
    out = dict(params or {})
    proof = whatsapp.appsecret_proof(keys.token, keys.app_secret)
    if proof:
        out["appsecret_proof"] = proof
    return headers, out


async def call(keys: Keys, method: str, path: str, *, params: Optional[dict] = None, json: Any = None,
               timeout: Optional[float] = None) -> dict:
    """One Graph call; Meta's JSON answer, or GraphError."""
    headers, query = _auth(keys, params)
    try:
        async with _client(timeout) as client:
            r = await client.request(method, url(path), headers=headers, params=query or None, json=json)
    except httpx.HTTPError as exc:
        raise GraphError(f"Meta could not be reached ({exc.__class__.__name__})", unreachable=True) from None
    if r.status_code >= 300:
        raise _error(r)
    try:
        out = r.json()
    except ValueError:
        out = {}
    return out if isinstance(out, dict) else {"data": out}


# ─── The number and the account ─────────────────────────────────────────


async def phone_number(keys: Keys) -> dict:
    """The number as Meta has it: display number, verified name, quality
    rating, messaging limit, status and display name's review. Asked again
    without `name_status` (a beta field) when Meta won't show it to the app."""
    try:
        return await call(keys, "GET", keys.phone_number_id, params={"fields": PHONE_FIELDS})
    except GraphError as exc:
        if exc.code != "100" or "name_status" not in (exc.message or ""):
            raise
        return await call(keys, "GET", keys.phone_number_id, params={"fields": PHONE_FIELDS_PLAIN})


async def debug_token(keys: Keys) -> dict:
    """What Meta says of the club's token (GET /debug_token, asked with the
    token itself): whether it is valid, its app, its permissions (`scopes`)
    and when it expires (0: never, as a system user's permanent token)."""
    out = await call(keys, "GET", "debug_token", params={"input_token": keys.token})
    return out.get("data") if isinstance(out.get("data"), dict) else {}


async def waba_numbers(keys: Keys) -> list[str]:
    """The ids of the numbers in the club's WhatsApp Business Account."""
    out = await call(keys, "GET", f"{keys.waba_id}/phone_numbers", params={"fields": "id", "limit": 100})
    return [str(n.get("id")) for n in out.get("data") or [] if isinstance(n, dict) and n.get("id")]


async def subscribe_app(keys: Keys) -> None:
    """Subscribes the club's app to its WhatsApp Business Account's webhooks:
    without it Meta sends the app nothing about that account."""
    await call(keys, "POST", f"{keys.waba_id}/subscribed_apps")


async def unsubscribe_app(keys: Keys) -> None:
    """The other way: Meta stops sending the app the account's webhooks."""
    await call(keys, "DELETE", f"{keys.waba_id}/subscribed_apps")


# ─── Templates ───────────────────────────────────────────────────────────


#: Meta (v24+): a template page asked for with a cursor it no longer knows.
STALE_CURSOR = "131059"


async def list_templates(keys: Keys, *, name: Optional[str] = None) -> list[dict]:
    """The account's templates (`name`: those of that name), page by page.
    A cursor Meta calls stale (131059) is dropped and the reading starts
    again, once, as Meta says to."""
    for attempt in (1, 2):
        out: list[dict] = []
        params: dict = {"fields": TEMPLATE_FIELDS, "limit": 100, **({"name": name} if name else {})}
        try:
            for _ in range(TEMPLATE_PAGES):
                page = await call(keys, "GET", f"{keys.waba_id}/message_templates", params=params)
                out.extend(t for t in page.get("data") or [] if isinstance(t, dict))
                after = ((page.get("paging") or {}).get("cursors") or {}).get("after")
                if not after or not (page.get("paging") or {}).get("next"):
                    break
                params = {**params, "after": after}
            return out
        except GraphError as exc:
            if exc.code != STALE_CURSOR or attempt == 2 or "after" not in params:
                raise
    return []


async def create_template(keys: Keys, body: dict) -> dict:
    return await call(keys, "POST", f"{keys.waba_id}/message_templates", json=body)


async def delete_template(keys: Keys, name: str, hsm_id: Optional[str] = None) -> None:
    params = {"name": name}
    if hsm_id:
        params["hsm_id"] = hsm_id
    await call(keys, "DELETE", f"{keys.waba_id}/message_templates", params=params)


async def upload_sample(keys: Keys, content: bytes, filename: str, mime: str) -> str:
    """A sample file for a template's media header, through Meta's resumable
    upload on the club's app: its handle ("4::..."), which the template's
    `example.header_handle` names."""
    if not keys.app_id:
        raise GraphError("The app id is needed to submit a template with a document header", code="APP_ID")
    session = await call(keys, "POST", f"{keys.app_id}/uploads",
                         params={"file_name": filename, "file_length": len(content), "file_type": mime})
    upload_id = str(session.get("id") or "")
    if not upload_id:
        raise GraphError("Meta did not open an upload for the sample", unreachable=True)
    headers = {"Authorization": f"OAuth {keys.token}", "file_offset": "0"}
    # As every Graph call with the token: an app that requires the app secret wants its proof here too.
    _headers, proof = _auth(keys, None)
    try:
        async with _client() as client:
            r = await client.post(url(upload_id), headers=headers, params=proof or None, content=content)
    except httpx.HTTPError as exc:
        raise GraphError(f"Meta could not be reached ({exc.__class__.__name__})", unreachable=True) from None
    if r.status_code >= 300:
        raise _error(r)
    try:
        answer = r.json()
    except ValueError:
        raise GraphError("Meta answered the sample's upload with something that isn't JSON", status=r.status_code,
                         unreachable=True) from None
    handle = str((answer if isinstance(answer, dict) else {}).get("h") or "")
    if not handle:
        raise GraphError("Meta took the sample without a handle", unreachable=True)
    return handle


# ─── Messages and media ──────────────────────────────────────────────────


async def send(keys: Keys, body: dict) -> str:
    """POST /{number}/messages: the wamid Meta gave the message. A refusal
    is one of whatsapp's errors (Undeliverable, Deferred, Refused), as for
    the platform's own sends; no answer at all is Unconfirmed."""
    headers, query = _auth(keys, None)
    try:
        async with _client() as client:
            r = await client.post(url(f"{keys.phone_number_id}/messages"), headers=headers, params=query or None, json=body)
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        raise whatsapp.Deferred(f"WhatsApp could not be reached ({exc.__class__.__name__})") from None
    except httpx.HTTPError as exc:
        raise whatsapp.Unconfirmed(f"WhatsApp did not answer ({exc.__class__.__name__}) — it may have gone") from None
    if r.status_code >= 300:
        try:
            answer = r.json()
        except ValueError:
            answer = {}
        raise whatsapp.refusal(r.status_code, answer)
    try:
        messages = (r.json() or {}).get("messages") or [{}]
    except ValueError:
        messages = [{}]
    return str((messages[0] or {}).get("id") or "") if isinstance(messages, list) else ""


async def mark_read(keys: Keys, wamid: str) -> None:
    await call(keys, "POST", f"{keys.phone_number_id}/messages",
               json={"messaging_product": "whatsapp", "status": "read", "message_id": wamid})


#: Where Meta's media URLs point (the lookaside host of a GET /{media id}):
#: the club's token is sent there and nowhere else.
MEDIA_HOSTS = frozenset({"lookaside.fbsbx.com"})


def media_host_ok(media_url: str) -> bool:
    try:
        parts = urlsplit(media_url)
    except ValueError:
        return False
    return parts.scheme == "https" and (parts.hostname or "").lower() in MEDIA_HOSTS and parts.port in (None, 443)


async def media_info(keys: Keys, media_id: str) -> dict:
    """Where Meta keeps an attachment for now (a URL good for five minutes), its type and size."""
    return await call(keys, "GET", media_id)


async def download(keys: Keys, media_url: str, max_bytes: int) -> bytes:
    """The attachment itself: Meta's URL answers only with the token, which
    goes to Meta's media host alone (MEDIA_HOSTS) -- an address anywhere
    else is refused before anything is sent."""
    if not media_host_ok(media_url):
        raise GraphError("Meta gave an attachment address that isn't its media host", code="MEDIA_HOST")
    headers = {"Authorization": f"Bearer {keys.token}"}
    try:
        async with _client(60.0) as client:
            async with client.stream("GET", media_url, headers=headers) as r:
                if r.status_code >= 300:
                    await r.aread()
                    raise _error(r)
                chunks, size = [], 0
                async for chunk in r.aiter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise GraphError("The attachment is larger than the inbox shows", code="TOO_LARGE")
                    chunks.append(chunk)
                return b"".join(chunks)
    except httpx.HTTPError as exc:
        raise GraphError(f"Meta could not be reached ({exc.__class__.__name__})", unreachable=True) from None


# ─── In words ────────────────────────────────────────────────────────────

#: What a refusal of the club's keys means, by Meta's code: (error code, sentence).
EXPLAINED = {
    "190": ("WHATSAPP_TOKEN_INVALID",
            "Meta didn't accept the access token: it's wrong, expired or revoked. Make a permanent token for a system user and paste it again."),
    "102": ("WHATSAPP_TOKEN_INVALID", "Meta didn't accept the access token. Make a permanent token for a system user and paste it again."),
    "10": ("WHATSAPP_PERMISSION_MISSING",
           "The token is missing a permission. Give it whatsapp_business_management and whatsapp_business_messaging."),
    "200": ("WHATSAPP_PERMISSION_MISSING",
            "The token is missing a permission. Give it whatsapp_business_management and whatsapp_business_messaging."),
    "3": ("WHATSAPP_PERMISSION_MISSING", "Your Meta app isn't allowed to use the WhatsApp API. Add the WhatsApp product to the app."),
    "368": ("WHATSAPP_ACCOUNT_BLOCKED",
            "Meta has restricted or disabled this WhatsApp Business Account for a policy violation. Check WhatsApp Manager."),
    "131064": ("WHATSAPP_ACCOUNT_BLOCKED",
               "Meta has limited this WhatsApp Business Account for templates in the wrong category. It lifts after a while; "
               "check the templates' categories in WhatsApp Manager."),
    "131031": ("WHATSAPP_ACCOUNT_BLOCKED", "Meta has locked this WhatsApp Business Account. Check WhatsApp Manager."),
    "131042": ("WHATSAPP_PAYMENT_PROBLEM", "The WhatsApp Business Account has a payment problem. Add or fix its payment method in WhatsApp Manager."),
    "133010": ("WHATSAPP_NUMBER_NOT_REGISTERED", "The number isn't registered with the WhatsApp Cloud API yet. Register it in your app's API setup."),
}


def explain(exc: GraphError) -> tuple[int, str, str]:
    """(HTTP status, error code, sentence) for a refusal of the club's keys or account."""
    message = exc.message or ""
    if exc.unreachable:
        return 502, "META_UNREACHABLE", "Couldn't reach Meta to check. Try again in a minute."
    if "appsecret_proof" in message.lower():
        return 422, "WHATSAPP_APP_SECRET_WRONG", "That app secret doesn't belong to the app the access token was made in. Copy it from App settings → Basic."
    if exc.code in EXPLAINED:
        code, words = EXPLAINED[exc.code]
        return 422, code, words
    if exc.code in ("100", "803") or "does not exist" in message.lower():
        return 422, "WHATSAPP_NUMBER_NOT_FOUND", "Meta doesn't know that ID, or this token can't see it. Check the WhatsApp Business Account ID and the phone number ID."
    return 422, "WHATSAPP_REFUSED", f"Meta refused it: {message}"[:300]


def is_template_error(exc: GraphError) -> bool:
    """Meta refusing a template itself (creating or deleting one): code 100,
    or one of its 2388xxx template codes (as the code or the subcode)."""
    return exc.code == "100" or any(str(c or "").startswith("2388") for c in (exc.code, exc.subcode))


def explain_template(exc: GraphError) -> tuple[int, str, str]:
    """(HTTP status, error code, sentence) for a template Meta wouldn't
    create or delete: INVALID_TEMPLATE in Meta's own words for a person
    (error_user_title, error_user_msg) when it is about the template --
    not "Meta doesn't know that ID" -- else what `explain` says of the keys."""
    if exc.unreachable or not is_template_error(exc) or "appsecret_proof" in (exc.message or "").lower():
        return explain(exc)
    words = ": ".join(w for w in (exc.title, exc.user_message) if w) or exc.message or "Meta didn't accept the template."
    return 422, "INVALID_TEMPLATE", f"Meta didn't accept the template. {words}"[:400]
