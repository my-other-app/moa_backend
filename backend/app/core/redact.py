"""Keeping secrets out of logs and error reports.

- A club's webhook URL ends in a token that is half of what lets a caller in
  (the signature is the other half): the access log and the error report get
  its first six characters.
- The routes where a club pastes its Razorpay keys carry a key secret in the
  body. Nothing here logs request bodies, but an error report about them
  gives the traceback's frames and the exception's type, never its message,
  which could quote what was sent.
"""
import logging
import re
import traceback

_CLUB_WEBHOOK = re.compile(r"(/payments/webhook/club/)([A-Za-z0-9_-]{0,6})[A-Za-z0-9_-]*")
_SENSITIVE_BODIES = re.compile(r"^/api/v1/clubs/payments/gateway(?:/|$)")


def path(value: str) -> str:
    return _CLUB_WEBHOOK.sub(lambda m: f"{m.group(1)}{m.group(2)}…", value)


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
    """uvicorn's access line, with a club webhook's token cut short."""

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
