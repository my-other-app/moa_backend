import traceback
import uuid
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import ORJSONResponse
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import HTMLResponse
from starlette import status
from sqlalchemy.exc import StatementError

from app.api.router import api_router
from app.config import settings
from app.db.listeners import *
from app.response import ErrorResponse, CustomHTTPException
from app.core.utils.discord import notify_error
from app.core.middlewares.process_time_middleware import ProcessingTimeMiddleware

application = FastAPI(default_response_class=ORJSONResponse)

application.include_router(router=api_router)
# print(settings.cors_origins)
# if settings.DEBUG:
application.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
application.add_middleware(ProcessingTimeMiddleware)

# @application.exception_handler(ValidationError)
# async def validation_exception_handler2(request, exc):
#     errors = {}

#     for error in exc.errors():
#         current = errors

#         if len(error["loc"]) <= 1:
#             current[error["loc"][0]] = error["msg"]
#             break

#         keys = error["loc"][1:]
#         for loc in keys[:-1]:
#             current = current.setdefault(loc, {})
#         current[keys[-1]] = error["msg"]

#     return ErrorResponse(
#         message="Invalid request",
#         errors=errors,
#     ).get_response(status.HTTP_422_UNPROCESSABLE_ENTITY)


@application.exception_handler(Exception)
async def http_exception_handler(request: Request, exc: Exception):
    track_id = str(uuid.uuid4())
    try:
        await notify_error(request, exc, track_id)
    except Exception as e:
        print("Error while sending error notification")
        traceback.print_exc()
    return ErrorResponse(
        message="Internal Server Error",
        errors={"error": "An error occurred while processing the request"},
        track_id=track_id,
    ).get_response(status.HTTP_500_INTERNAL_SERVER_ERROR)


@application.exception_handler(StatementError)
async def statement_error_handler(request: Request, exc: StatementError):
    if isinstance(exc.orig, CustomHTTPException):
        raise exc.orig
    else:
        raise exc


@application.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc):
    errors = {}

    for error in exc.errors():
        current = errors

        if len(error["loc"]) <= 1:
            current[error["loc"][0]] = error["msg"]
            break

        keys = error["loc"][1:]
        for loc in keys[:-1]:
            current = current.setdefault(loc, {})
        current[keys[-1]] = error["msg"]

    return ErrorResponse(
        message="Invalid request",
        errors=errors,
    ).get_response(status.HTTP_422_UNPROCESSABLE_ENTITY)


@application.exception_handler(CustomHTTPException)
async def http_exception_handler(request: Request, exc: CustomHTTPException):
    return exc.get_response(exc.status_code)


@application.head("/ping")
async def ping():
    return HTMLResponse(content=None, status_code=status.HTTP_204_NO_CONTENT)
