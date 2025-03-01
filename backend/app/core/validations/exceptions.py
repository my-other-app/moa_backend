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
