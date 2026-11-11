"""The Integrations API's one error shape:

    HTTP status + {"error": {"code", "message", "details"?, "request_id"}}

`PartnerError` is what this module raises. A refusal from a domain service
(a CustomHTTPException: EVENT_FULL, TICKET_TYPE_SOLD_OUT, the plan's limits)
keeps its code where it has one, with the status the contract gives it; one
without a code is named by its status. FastAPI's own validation errors are
VALIDATION, with each field's message in `details.fields`.
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Optional

from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

#: The codes the contract names, and the status each answers with.
CODES = {
    "UNAUTHENTICATED": 401,
    "FORBIDDEN_SCOPE": 403,
    "NOT_ALLOCATED": 403,
    "CLUB_NOT_ACTIVE": 403,
    "APP_SUSPENDED": 403,
    "NOT_CONNECTED": 403,
    "IP_NOT_ALLOWED": 403,
    "NOT_FOUND": 404,
    "VALIDATION": 422,
    "ALREADY_EXISTS": 409,
    "IDEMPOTENCY_MISMATCH": 422,
    "IDEMPOTENCY_IN_PROGRESS": 409,
    "PRECONDITION_FAILED": 412,
    "PAYMENT_REQUIRED": 409,
    "EXTERNAL_PAYMENTS_OFF": 409,
    "CANCEL_IN_DASHBOARD": 409,
    "EVENT_FULL": 409,
    "REGISTRATION_CLOSED": 409,
    "ALREADY_REGISTERED": 409,
    "ALREADY_CHECKED_IN": 409,
    "INVALID_STATE": 409,
    "EVENT_NOT_OPEN": 409,
    "EVENT_CANCELLED": 409,
    "EVENT_LOCKED": 409,
    "TICKET_REVOKED": 409,
    "TICKET_TYPE_SOLD_OUT": 409,
    "TICKET_TYPE_NOT_ON_SALE": 409,
    "TICKET_TYPE_REQUIRED": 422,
    "TICKET_TYPE_NOT_FOUND": 422,
    "DISCLOSURE_REQUIRED": 422,
    "CONFLICT": 409,
    "FORBIDDEN": 403,
    "RATE_LIMITED": 429,
    "INTERNAL": 500,
}

#: Domain codes said the contract's way.
_RENAMED = {
    "REGISTRATIONS_CLOSED": "REGISTRATION_CLOSED",
    "EVENT_NOT_FOUND": "NOT_FOUND",
    "APP_NOT_FOUND": "NOT_FOUND",
}

_BY_STATUS = {
    400: "VALIDATION",
    401: "UNAUTHENTICATED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    412: "PRECONDITION_FAILED",
    413: "VALIDATION",
    415: "VALIDATION",
    422: "VALIDATION",
    429: "RATE_LIMITED",
}


class PartnerError(CustomHTTPException):
    """A refusal on the Integrations API, already in its own terms."""

    def __init__(self, code: str, message: str, *, status: Optional[int] = None, details: Optional[dict] = None, headers: Optional[dict] = None):
        super().__init__(status or CODES.get(code, 400), message, error_code=code, errors=details, headers=headers)
        self.code = code
        self.details = details


def not_found(what: str = "That") -> PartnerError:
    return PartnerError("NOT_FOUND", f"{what} was not found.")


def request_id(request) -> str:
    rid = getattr(request.state, "partner_request_id", None)
    if not rid:
        from app.core import observability

        rid = observability.current_request_id() or f"req_{uuid.uuid4().hex}"
        request.state.partner_request_id = rid
    return rid


def body(code: str, message: str, request, details: Optional[dict] = None) -> dict:
    error = {"code": code, "message": message}
    if details:
        error["details"] = details
    error["request_id"] = request_id(request)
    return {"error": error}


def _validation_details(exc: RequestValidationError) -> dict:
    fields: dict = {}
    for err in exc.errors():
        loc = [str(p) for p in err.get("loc", ()) if p not in ("body", "query", "path", "header")]
        fields[".".join(loc) or "body"] = err.get("msg", "Invalid value")
    return {"fields": fields}


def translate(exc: Exception) -> tuple[int, str, str, Optional[dict], dict]:
    """(status, code, message, details, headers) for anything a partner route raised."""
    if isinstance(exc, PartnerError):
        return exc.status_code, exc.code, exc.message, exc.details, dict(exc.headers or {})
    if isinstance(exc, RequestValidationError):
        return 422, "VALIDATION", "The request is not valid.", _validation_details(exc), {}
    if isinstance(exc, CustomHTTPException):
        raw = exc.error_code or ""
        code = _RENAMED.get(raw, raw) or _BY_STATUS.get(exc.status_code, "ERROR")
        status = CODES.get(code, exc.status_code)
        if status == 400:
            status = 422
        details = exc.errors if isinstance(exc.errors, dict) else ({"errors": exc.errors} if exc.errors else None)
        return status, code, exc.message or "The request was refused.", details, dict(exc.headers or {})
    if isinstance(exc, HTTPException):
        message = exc.detail if isinstance(exc.detail, str) else "The request was refused."
        return exc.status_code, _BY_STATUS.get(exc.status_code, "ERROR"), message, None, dict(exc.headers or {})
    from sqlalchemy.exc import IntegrityError

    if isinstance(exc, IntegrityError):
        return 409, "CONFLICT", "That conflicts with something that already exists.", None, {}
    raise exc


def render(request, exc: Exception) -> JSONResponse:
    status, code, message, details, headers = translate(exc)
    return JSONResponse(body(code, message, request, details), status_code=status, headers=headers)


def internal(request, exc: Exception) -> JSONResponse:
    """The 500: reported as the app's own are, answered in this API's shape."""
    rid = request_id(request)
    try:
        from app.core.utils.discord import report_error

        report_error(request, exc, rid)
    except Exception:  # noqa: BLE001
        logger.warning("could not report a partner API error", exc_info=True)
    logger.exception("partner API request %s failed", rid)
    return JSONResponse(body("INTERNAL", "Something went wrong on our side. Try again; quote the request id if it keeps happening.", request), status_code=500)


def dumps(payload) -> str:
    return json.dumps(payload, separators=(",", ":"), default=str)
