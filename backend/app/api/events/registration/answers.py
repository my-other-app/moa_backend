"""Answers to an event's own registration questions, checked before they are
kept.

Every set of answers is bounded (how many, how long, how deep), whether or
not the event asks anything: with no questions the answers used to be
stored as sent, any size, any shape. Against the event's questions each
answer is checked for its type: one of the options for a choice, the
options ticked for a checkbox (one, or a list of them), a number, an
email address, a web address, a date.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from urllib.parse import urlparse

from app.response import CustomHTTPException

#: Bounds on any set of answers.
MAX_KEYS = 50
MAX_KEY_LENGTH = 100
MAX_TEXT = 2000
MAX_LIST = 50
MAX_BYTES = 16_000

_EMAIL = re.compile(r"^[^\s@]+@[^\s@.]+(\.[^\s@.]+)*\.[^\s@.]{2,}$")
_NUMBER = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")
#: What a date question is answered with: the date picker's ISO form, and
#: what people type into a plain text box in India.
_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y", "%d %b %Y", "%d %B %Y")
_SCALARS = (str, int, float, bool)


def _refuse(errors: dict, message: str = "Check your answers.") -> CustomHTTPException:
    return CustomHTTPException(400, message=message, error_code="INVALID_ANSWERS", errors=errors)


def bound(answers) -> dict:
    """The answers, or 400 when there are too many, too long, or nested."""
    if answers is None:
        return {}
    if not isinstance(answers, dict):
        raise _refuse({"additional_details": "Answers are a set of question and answer."})
    if len(answers) > MAX_KEYS:
        raise _refuse({"additional_details": f"At most {MAX_KEYS} answers."}, f"Too many answers: at most {MAX_KEYS}.")
    errors = {}
    for key, value in answers.items():
        if not isinstance(key, str) or not key.strip() or len(key) > MAX_KEY_LENGTH:
            errors[str(key)[:MAX_KEY_LENGTH]] = "Not a question this form asks."
            continue
        values = value if isinstance(value, list) else [value]
        if isinstance(value, list) and len(value) > MAX_LIST:
            errors[key] = f"At most {MAX_LIST} choices."
        elif any(v is not None and not isinstance(v, _SCALARS) for v in values):
            errors[key] = "One answer, or a list of them."
        elif any(isinstance(v, str) and len(v) > MAX_TEXT for v in values):
            errors[key] = f"Keep it under {MAX_TEXT} characters."
    if errors:
        raise _refuse(errors)
    if len(json.dumps(answers, default=str)) > MAX_BYTES:
        raise _refuse({"additional_details": "The answers are too long."}, "The answers are too long.")
    return answers


def _blank(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip()) or (isinstance(value, list) and not [v for v in value if not _blank(v)])


def _field_type(field) -> str:
    # CustomBaseModel sets use_enum_values=True, so the type may be the string.
    return getattr(field.field_type, "value", field.field_type)


def _looks_like_url(text: str) -> bool:
    v = text.strip()
    if not v or re.search(r"\s", v):
        return False
    try:
        parsed = urlparse(v if re.match(r"^[a-z][a-z0-9+.-]*:", v, re.I) else f"https://{v}")
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and "." in (parsed.hostname or "")


def _is_date(text: str) -> bool:
    v = text.strip()
    for fmt in _DATE_FORMATS:
        try:
            datetime.strptime(v, fmt)
            return True
        except ValueError:
            continue
    return False


def _problem(field, value) -> str | None:
    """What is wrong with one answer, or None."""
    kind = _field_type(field)
    options = [o for o in (field.options or []) if o is not None]
    if kind == "checkbox":
        picked = value if isinstance(value, list) else [value]
        if not options:
            # A lone tick box ("I agree"): ticked is enough.
            return None if all(v is True or (isinstance(v, str) and v.strip()) for v in picked) else "Tick this box to continue."
        if any(v not in options for v in picked):
            return "Invalid option selected"
        if len(set(map(str, picked))) != len(picked):
            return "Each option once."
        return None
    if isinstance(value, list):
        return "One answer."
    if kind in ("select", "radio"):
        return None if value in options else "Invalid option selected"
    if isinstance(value, bool):
        return "Not an answer to this question."
    text = str(value).strip()
    if kind == "number":
        return None if isinstance(value, (int, float)) or _NUMBER.match(text) else "Enter a number."
    if kind == "email":
        return None if _EMAIL.match(text) else "Enter an email address, like name@example.com."
    if kind == "url":
        return None if _looks_like_url(text) else "Enter a web address, like example.com."
    if kind == "date":
        return None if _is_date(text) else "Enter a date, like 2026-10-18."
    return None


def check(questions, answers, *, relaxed: bool = False) -> dict | None:
    """The answers to keep. `relaxed` is the door's: nothing is required,
    blanks are dropped, and what was answered is kept as it came (bounded).
    With no questions, the answers are kept as they came (bounded).
    Otherwise the answers to the event's questions, each checked; a
    required one missing, or one of the wrong type, is a 400 naming them."""
    answers = bound(answers)
    if relaxed:
        return {k: v for k, v in answers.items() if not _blank(v)}
    if not questions:
        return answers or None
    from app.api.events.schemas import EventAdditionalDetail

    fields = [EventAdditionalDetail.model_validate(q) for q in questions]
    errors: dict = {}
    kept: dict = {}
    for field in fields:
        value = answers.get(field.key)
        if _blank(value):
            if field.required:
                errors[field.key] = "This field is required"
            continue
        why = _problem(field, value)
        if why:
            errors[field.key] = why
            continue
        kept[field.key] = value
    if errors:
        labels = [f.label for f in fields if f.key in errors]
        raise _refuse(errors, f"Check your answers: {', '.join(labels)}.")
    return kept
