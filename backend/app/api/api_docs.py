"""The API's own docs: /docs (Swagger UI), /redoc and /openapi.json, the
whole route map. For development, not for the public.

settings.API_DOCS (APP_API_DOCS) says whether they are served. Unset, they
follow the deployment: off in production (app.core.config_checks.
is_production: an ENV other than development, dev, local, test or ci, or no
ENV with DEBUG off), on anywhere else, the suite included. true or false
says so outright. It is read on every request, so a test can turn it.

While they are off, every one of them answers 404, the same as a path that
does not exist, with one exception: /openapi.json still answers the website's
own server (a valid X-Site-Token, app.core.site). The partner API's document
(GET /partner/v1/openapi.json) has a gate of its own and is not this.

FastAPI's own docs routes are switched off (doth.py: docs_url, redoc_url and
openapi_url are None) and these stand in for them, so the choice is made per
request rather than once when the app is built.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html, get_swagger_ui_oauth2_redirect_html
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import settings
from app.core import site
from app.core.config_checks import is_production

OPENAPI_URL = "/openapi.json"
DOCS_URL = "/docs"
REDOC_URL = "/redoc"
OAUTH2_REDIRECT_URL = "/docs/oauth2-redirect"

router = APIRouter(include_in_schema=False)


def enabled() -> bool:
    """Whether the docs are served to anyone."""
    if settings.API_DOCS is not None:
        return bool(settings.API_DOCS)
    return not is_production(settings)


def _not_found() -> StarletteHTTPException:
    # What the router answers for a path it does not know.
    return StarletteHTTPException(status_code=404)


@router.get(OPENAPI_URL)
async def openapi(request: Request) -> JSONResponse:
    if not enabled() and site.token_id(request) is None:
        raise _not_found()
    return JSONResponse(request.app.openapi())


@router.get(DOCS_URL)
async def swagger_ui(request: Request) -> HTMLResponse:
    if not enabled():
        raise _not_found()
    app = request.app
    return get_swagger_ui_html(
        openapi_url=OPENAPI_URL,
        title=f"{app.title} - Swagger UI",
        oauth2_redirect_url=OAUTH2_REDIRECT_URL,
        init_oauth=app.swagger_ui_init_oauth,
        swagger_ui_parameters=app.swagger_ui_parameters,
    )


@router.get(OAUTH2_REDIRECT_URL)
async def swagger_ui_redirect() -> HTMLResponse:
    if not enabled():
        raise _not_found()
    return get_swagger_ui_oauth2_redirect_html()


@router.get(REDOC_URL)
async def redoc(request: Request) -> HTMLResponse:
    if not enabled():
        raise _not_found()
    return get_redoc_html(openapi_url=OPENAPI_URL, title=f"{request.app.title} - ReDoc")
