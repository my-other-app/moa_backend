from fastapi import HTTPException
from fastapi.responses import JSONResponse


class ErrorResponse:
    def __init__(
        self,
        message: str,
        error_code: str | None = None,
        track_id: str | None = None,
        errors: dict | None = None,
    ):
        self.message = message
        self.error_code = error_code
        self.track_id = track_id
        self.errors = errors

    def to_dict(self):
        return {
            "message": self.message,
            "errors": self.errors,
            "error_code": self.error_code,
            "track_id": self.track_id,
        }

    def get_response(self, status):
        return JSONResponse(
            content=self.to_dict(),
            status_code=status,
        )

    def __str__(self):
        return self.message

    def __repr__(self):
        return self.message


class CustomHTTPException(HTTPException, ErrorResponse):

    def __init__(
        self,
        status_code,
        message,
        error_code=None,
        track_id=None,
        errors=None,
        *args,
        **kwargs
    ):
        ErrorResponse.__init__(self, message, error_code, track_id, errors)
        super().__init__(status_code=status_code, detail=message, *args, **kwargs)
