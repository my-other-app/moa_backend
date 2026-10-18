"""Emailed links that no longer work: the page a person gets, and a fresh link by email.

A ticket or certificate link carries an HMAC (app/core/mail/links.py). When
its key is gone -- rotated out, or the one that leaked in October 2026,
which is never accepted again -- or the ticket is gone, an app or a script
still gets the JSON error it always did (403, 404, 410), but a person who
tapped the link in their email gets a small page with the same status. It
says the link no longer works, where the ticket still is (the app, and My
tickets on the site), and offers a form: the address they registered with,
and a fresh link goes there.

Who is a person: an `Accept` header that names text/html and ranks it above
application/json -- what every browser sends when it follows a link. curl,
httpx, fetch() and the apps send */* or application/json, and get JSON.

The fresh link (`resend`) never says whether anything matched: the answer
is the same sentence whatever was typed. It is sent only when the code and
the address are a live registration's, only to that registration's own
address, through the delivery queue a ticket always goes through
(app/api/deliveries, a job), at most once in ten minutes a ticket. Each ask
is counted per address and per ticket code (the limiter, which both forget
on their own) and written to the audit log.
"""
from __future__ import annotations

import base64
import hashlib
import html
import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional
from urllib.parse import urlsplit

from fastapi.responses import HTMLResponse
from limits import parse_many
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from app.api.audit import service as audit
from app.api.deliveries import service as deliveries
from app.api.deliveries.models import DeliveryKind
from app.api.events.models import EventRegistrationsLink, Events
from app.core.net import client_address
from app.core.public_urls import site_url
from app.core.rate_limit import limiter

logger = logging.getLogger(__name__)

#: The one answer to every ask for a fresh link, whatever matched.
GENERIC = "If that email has a ticket for this event, we've sent a fresh link."
#: Asks for a fresh link: per address, and per ticket code however many
#: addresses ask for it. The code is what was typed, so a refusal says
#: nothing about whether the ticket exists.
PER_ADDRESS = "5/minute;20/hour"
PER_TICKET = "3/hour"
#: The longest ticket code accepted (they are 15 characters; legacy ones a few more).
CODE_MAX = 64
EMAIL_MAX = 254


# ─── Who is asking ───────────────────────────────────────────────────────


def wants_html(request) -> bool:
    """Whether the caller asked for a page: text/html named, with a higher
    weight than application/json. `*/*` alone is not asking for one."""
    weights = {"text/html": 0.0, "application/json": 0.0}
    for part in (request.headers.get("accept") or "").split(","):
        media, *params = [p.strip() for p in part.split(";")]
        media = media.lower()
        if media not in weights:
            continue
        q = 1.0
        for p in params:
            name, _, value = p.partition("=")
            if name.strip().lower() == "q":
                try:
                    q = float(value)
                except ValueError:
                    q = 0.0
        weights[media] = max(weights[media], q)
    return weights["text/html"] > 0 and weights["text/html"] > weights["application/json"]


# ─── The page ────────────────────────────────────────────────────────────

_CSS = (
    ":root{color-scheme:light dark;--bg:#f6f5f2;--card:#fff;--ink:#1d1d1b;--soft:#5f5e5a;--line:#e3e1dc;--accent:#0f7a6c}"
    "@media (prefers-color-scheme:dark){:root{--bg:#161615;--card:#20201e;--ink:#f0efea;--soft:#a9a7a0;--line:#34332f;--accent:#4cc3b1}}"
    "*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);"
    "font:16px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif}"
    "main{max-width:440px;margin:48px auto;padding:0 16px}"
    ".card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:28px 24px}"
    "h1{font-size:22px;line-height:1.3;margin:0 0 8px}p{margin:0 0 16px;color:var(--soft)}"
    "a{color:var(--accent)}hr{border:0;border-top:1px solid var(--line);margin:24px 0}"
    "label{display:block;font-weight:600;margin:0 0 6px}"
    "input[type=email]{width:100%;font:inherit;padding:10px 12px;border:1px solid var(--line);border-radius:10px;"
    "background:var(--bg);color:var(--ink)}"
    "button{margin-top:12px;width:100%;font:inherit;font-weight:600;padding:11px 14px;border:0;border-radius:10px;"
    "background:var(--accent);color:#fff;cursor:pointer}"
    ".note{font-size:14px}.error{color:#b42318}.brand{font-weight:700;letter-spacing:.2px;margin:0 0 16px;color:var(--ink)}"
)


def _style_hash() -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(_CSS.encode()).digest()).decode() + "'"


@lru_cache(maxsize=1)
def _csp() -> str:
    # No script at all; the stylesheet by its hash; the form posts here only.
    return f"default-src 'none'; style-src {_style_hash()}; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"


@dataclass(frozen=True)
class Copy:
    title: str
    body: str
    form: bool


_COPY = {
    "invalid": Copy(
        "This link no longer works",
        "The link in your email has expired or is no longer valid.",
        True,
    ),
    "revoked": Copy(
        "This ticket is no longer valid",
        "The organiser has cancelled it. If you think that is a mistake, reply to the organiser's email.",
        False,
    ),
    "unavailable": Copy(
        "This certificate is not available",
        "It has not been issued yet, or the organiser has withdrawn it.",
        False,
    ),
    "sent": Copy("Check your inbox", GENERIC + " It can take a few minutes to arrive.", False),
    "limited": Copy("Too many tries", "Wait a little while, then try again.", False),
}


def _site_name() -> str:
    return urlsplit(site_url()).hostname or "myotherapp.in"


def page(
    *, reason: str, kind: str, code: str, action: str, status: int, error: Optional[str] = None, email: str = "",
) -> HTMLResponse:
    """The page for `reason` (a key of _COPY), with `status` -- the one an
    app gets for the same request."""
    copy = _COPY[reason]
    e = html.escape
    thing = "certificate" if kind == "certificate" else "ticket"
    where = (
        f'<p>If you registered, your {thing} is in the <strong>MyOtherApp</strong> app, and at '
        f'<a href="{e(site_url("/me/tickets"))}">{e(_site_name())}</a> under My tickets once you sign in '
        f"with the email you registered with.</p>"
    )
    form = ""
    if copy.form:
        form = (
            "<hr>"
            f"<form method=\"post\" action=\"{e(action)}\">"
            f'<input type="hidden" name="kind" value="{e(kind)}">'
            f'<input type="hidden" name="code" value="{e(code)}">'
            '<label for="email">Get a fresh link by email</label>'
            f'<p class="note">Enter the email you registered with and we will send your {thing} again.</p>'
            + (f'<p class="note error" role="alert">{e(error)}</p>' if error else "")
            + f'<input id="email" name="email" type="email" autocomplete="email" required maxlength="{EMAIL_MAX}" value="{e(email)}">'
            "<button type=\"submit\">Send me a fresh link</button>"
            "</form>"
        )
    body = (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="robots" content="noindex">'
        f"<title>{e(copy.title)} - MyOtherApp</title><style>{_CSS}</style></head>"
        f'<body><main><div class="card"><p class="brand">MyOtherApp</p><h1>{e(copy.title)}</h1><p>{e(copy.body)}</p>'
        f"{where}{form}</div></main></body></html>"
    )
    return HTMLResponse(body, status_code=status, headers={
        "Content-Security-Policy": _csp(),
        "Cache-Control": "no-store",
        "X-Robots-Tag": "noindex",
    })


def resend_action(request) -> str:
    """Where the page's form posts: /…/files/resend beside the file route it is on."""
    path = request.url.path
    base = path.rsplit("/files/", 1)[0] if "/files/" in path else path.rsplit("/", 1)[0]
    return f"{base}/files/resend"


# ─── A fresh link ────────────────────────────────────────────────────────


@lru_cache(maxsize=None)
def _items(rate: str):
    return tuple(parse_many(rate))


def _spent(rate: str, scope: str, who: str) -> bool:
    """Count one ask against `rate` for `who`; True when it is over."""
    for item in _items(rate):
        if not limiter.limiter.hit(item, scope, who):
            return True
    return False


def over_limit(request, code: str) -> bool:
    """Count this ask per address and per ticket code; True when either is spent."""
    if not limiter.enabled:
        return False
    if _spent(PER_ADDRESS, "files:resend:address", client_address(request) or "unknown"):
        return True
    return bool(code) and _spent(PER_TICKET, "files:resend:ticket", code.upper())


def _normal(email: Optional[str]) -> str:
    return (email or "").strip().lower()


async def resend(session, request, *, kind: str, code: str, email: str) -> None:
    """The fresh link, when `code` and `email` are a live registration's for
    `kind`; nothing otherwise. Says nothing either way: the caller answers
    GENERIC whatever happened. The audit log has every ask."""
    reg = await session.scalar(
        select(EventRegistrationsLink)
        .options(joinedload(EventRegistrationsLink.event).joinedload(Events.club))
        .where(EventRegistrationsLink.ticket_id == code, EventRegistrationsLink.is_deleted == False)  # noqa: E712
    )
    event = reg.event if reg is not None else None
    # Read now: a failed send below rolls back, and expires them.
    reg_id, found = (reg.id if reg is not None else None), reg is not None
    matched = bool(
        reg is not None and event is not None and not event.is_deleted
        and reg.email and _normal(reg.email) == _normal(email)
    )
    sent = False
    if matched:
        # To the registration's own address, through the queue every ticket
        # goes through; refused (False) inside its ten-minute gap, or when
        # there is nothing to send (revoked, no certificate issued). A
        # failure is logged, not raised: a 500 only on a match would say
        # which address is the right one.
        try:
            sent = await deliveries.resend_document(session, event, reg, DeliveryKind(kind))
        except Exception:  # noqa: BLE001
            logger.exception("a fresh %s link for %s could not be queued", kind, code)
            await session.rollback()
    outcome = "sent" if sent else "matched, not sent" if matched else "wrong address" if found else "no such ticket"
    await audit.record(
        session, actor=None, action=f"{kind}_link.resend", entity_type="registration", entity_id=reg_id,
        summary=f"A fresh {kind} link was asked for {code[:CODE_MAX]}: {outcome}",
        details={"kind": kind, "code": code[:CODE_MAX], "found": found, "matched": matched, "sent": sent},
        request=request,
    )
    await session.commit()
    if sent:
        logger.info("a fresh %s link for %s was queued", kind, code)
