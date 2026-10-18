from app.response import CustomHTTPException


class RequestValidationError(CustomHTTPException):
    def __init__(self, *args, **kwargs):
        super().__init__(
            status_code=400,
            message="Invalid Request",
            error_code=None,
            track_id=None,
            errors=kwargs,
            *args
        )


class CodedRequestError(RequestValidationError):
    """A RequestValidationError that says what it is: its own status, a
    sentence for the person, and an error_code a client can switch on, with
    the field errors kept in `errors`. Still a RequestValidationError, so the
    code that catches those (a club's webhook skipping a payment it cannot
    apply) catches these too."""

    def __init__(self, status_code: int, message: str, error_code: str, **errors):
        CustomHTTPException.__init__(
            self, status_code=status_code, message=message, error_code=error_code, errors=errors or None
        )
