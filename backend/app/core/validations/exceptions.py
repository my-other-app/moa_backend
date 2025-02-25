from app.response import CustomHTTPException


class RequestValidationError(CustomHTTPException):
    def __init__(self, *args, **kwargs):
        super().__init__(400, "Invalid Request", None, None, kwargs, *args)
