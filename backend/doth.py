from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic_core import ValidationError
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import HTMLResponse
from starlette import status
from app.api.router import api_router
from app.config import settings
from app.db.listeners import *
from app.response import ErrorResponse, CustomHTTPException
from starlette.exceptions import HTTPException as StarletteHTTPException

application = FastAPI()

application.include_router(router=api_router)


application.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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
