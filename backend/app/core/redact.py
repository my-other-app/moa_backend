"""Keeping secrets out of logs and error reports.

- A club's webhook URL ends in a token that is half of what lets a caller in
  (the signature is the other half): the access log and the error report get
  its first six characters.
- The routes where a club pastes its Razorpay keys, or its own AI key,
  carry a secret in the body. Nothing here logs request bodies, but an
  error report about them gives the traceback's frames and the exception's
  type, never its message, which could quote what was sent. So do a club's
  own WhatsApp routes (its access token and app secret, and every message
  sent or read through its inbox) and its WhatsApp webhook (the messages
  people send it): no message content in a log or an error report. The
  inbox's search term (`q`, a name or a number) is [Filtered] wherever its
  URL is logged. A club's mailbox the same: its routes and its webhook
  carry mail, and its search (`q`) is filtered from the access log; and
  the platform's own inbox in the admin portal (/admin/mail).
- The live room's stream (EventSource cannot send headers) carries the
  browser's key and the player's key in its query string, and a player key
  is enough to answer as that player: `key`, `player` and, beside them,
  `last_event_id` -- and any `...token...` parameter -- are written as
  [Filtered] wherever a URL is logged (the access line, JSON or not, and
  Sentry's events through observability.scrub_event). So are the signed
  parameters of emailed links (`s`, `k`, `t`, `sig`), the secret segment of
  a link's path (an invite, a request, an offer, a cancel link...), and a
  ticket id, wherever it appears in a logged path.
- A hidden ticket type's link carries the key that sells it
  (`ticket_key`, app/api/ticketing/hidden.py): filtered the same way.
"""
import logging
import re
import traceback

_CLUB_WEBHOOK = re.compile(r"(/payments/webhook/club/|/hooks/whatsapp/club/)([A-Za-z0-9_-]{0,6})[A-Za-z0-9_-]*")
_SENSITIVE_BODIES = re.compile(r"^/api/v1/(?:clubs/(?:payments/gateway|ai/key|whatsapp|mailbox)|hooks/(?:whatsapp/club|mailbox)|admin/mail)(?:/|$)")
#: A query parameter whose value must not be logged, and its value (up to
#: the next parameter, fragment, space or quote). Besides the live room's:
#: the short signed parameters of the links people get -- `s` (a ticket or
#: certificate file), `k` (the missing-answers and feedback pages), `t` (the
#: recap unsubscribe and developer docs links) and `sig` (a partner's QR
#: image).
SECRET_QUERY = re.compile(r"(?i)([?&;](?:key|ticket_key|player|last_event_id|s|k|t|sig|\w*token\w*)=)[^&;#\s\"']*")
FILTERED = "[Filtered]"
#: The routes whose next path segment is the link's own secret: a club
#: invite, an assistant's pending action, a registration request, a speaker
#: claim, a support survey, a ticket's cancel link, a waiting-list offer, a
#: profile's QR code, a data deletion's status code. ("mine" is a route.)
_PATH_TOKEN = re.compile(
    r"(/(?:clubs/access/invite|clubs/pending-actions|registration-requests|speakers/claim|support/survey"
    r"|tickets/cancel|waitlist/offers|profiles/by-code|social/data-deletion)/)(?!mine(?:[/?#\s\"']|$))[^/?#\s\"']+"
)
#: A ticket id (MOA-XXXXX-XXXXX, and the older MOA-<hex time>-XXXXX): what
#: the door accepts, in the ticket, certificate and file routes' paths.
TICKET_ID = re.compile(r"\bMOA-[0-9A-Z]{4,16}-[0-9A-Z]{4,6}\b")
TICKET = "[ticket]"


def query(value: str) -> str:
    """`value` (a path, a URL, a log line) with every secret query value filtered."""
    return SECRET_QUERY.sub(lambda m: m.group(1) + FILTERED, value)


#: A search of a club's WhatsApp inbox (`q`: a name, a part of a number):
#: what the club looked for is the person it looked for, and is not logged.
_INBOX_SEARCH = re.compile(r"(/(?:clubs/(?:whatsapp|mailbox)|admin/mail)/[^\s\"'?#]*\?(?:[^\s\"'#]*?[&;])?q=)[^&;#\s\"']*")


#: A platform's sign-in coming back (app/api/social/oauth.py): its one-time
#: code and state are never logged.
_OAUTH_CALLBACK = {
    name: re.compile(rf"(/social/oauth/[a-z_]+/callback\?(?:[^\s\"'#]*?[&;])??{name}=)[^&;#\s\"']*")
    for name in ("code", "state")
}


def path_tokens(value: str) -> str:
    """`value` with a link's secret path segment filtered (an invite, an offer, a cancel link...)."""
    return _PATH_TOKEN.sub(lambda m: m.group(1) + FILTERED, value)


def path(value: str) -> str:
    value = _INBOX_SEARCH.sub(lambda m: m.group(1) + FILTERED, value)
    for rx in _OAUTH_CALLBACK.values():
        value = rx.sub(lambda m: m.group(1) + FILTERED, value)
    value = TICKET_ID.sub(TICKET, path_tokens(value))
    return query(_CLUB_WEBHOOK.sub(lambda m: f"{m.group(1)}{m.group(2)}…", value))


def is_sensitive(value: str) -> bool:
    return bool(_SENSITIVE_BODIES.search(value or ""))


def error_traceback(request_path: str, exc: BaseException) -> str:
    """The traceback for an error report: in full, except on a route whose
    body holds a secret, where the message is withheld."""
    if not is_sensitive(request_path):
        return path("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    frames = "".join(traceback.format_tb(exc.__traceback__))
    return f"Traceback (most recent call last):\n{frames}{type(exc).__name__}: (message withheld: this route carries a secret)\n"


class AccessLogFilter(logging.Filter):
    """uvicorn's access line, with a club webhook's token cut short and the
    secret query values filtered (its path argument carries the query)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and record.args:
            record.args = tuple(path(a) if isinstance(a, str) else a for a in record.args)
        elif isinstance(record.msg, str):
            record.msg = path(record.msg)
        return True


def install() -> None:
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, AccessLogFilter) for f in access.filters):
        access.addFilter(AccessLogFilter())
