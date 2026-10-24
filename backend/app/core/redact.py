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
  URL is logged.
- The live room's stream (EventSource cannot send headers) carries the
  browser's key and the player's key in its query string, and a player key
  is enough to answer as that player: `key`, `player` and, beside them,
  `last_event_id` -- and any `...token...` parameter -- are written as
  [Filtered] wherever a URL is logged (the access line, JSON or not, and
  Sentry's events through observability.scrub_event).
"""
import logging
import re
import traceback

_CLUB_WEBHOOK = re.compile(r"(/payments/webhook/club/|/hooks/whatsapp/club/)([A-Za-z0-9_-]{0,6})[A-Za-z0-9_-]*")
_SENSITIVE_BODIES = re.compile(r"^/api/v1/(?:clubs/(?:payments/gateway|ai/key|whatsapp)|hooks/whatsapp/club)(?:/|$)")
#: A query parameter whose value must not be logged, and its value (up to
#: the next parameter, fragment, space or quote).
SECRET_QUERY = re.compile(r"(?i)([?&;](?:key|player|last_event_id|\w*token\w*)=)[^&;#\s\"']*")
FILTERED = "[Filtered]"


def query(value: str) -> str:
    """`value` (a path, a URL, a log line) with every secret query value filtered."""
    return SECRET_QUERY.sub(lambda m: m.group(1) + FILTERED, value)


#: A search of a club's WhatsApp inbox (`q`: a name, a part of a number):
#: what the club looked for is the person it looked for, and is not logged.
_INBOX_SEARCH = re.compile(r"(/clubs/whatsapp/[^\s\"'?#]*\?(?:[^\s\"'#]*?[&;])?q=)[^&;#\s\"']*")


def path(value: str) -> str:
    value = _INBOX_SEARCH.sub(lambda m: m.group(1) + FILTERED, value)
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
