"""GET /api/v1/developers/access: the website asks whether a docs token is
good (docs_links.py; feature-register/private-dev-docs-contract.md).

Public, and not in any OpenAPI document. The token comes in the
`MOA-Docs-Token` header (preferred: it stays out of access logs) or as
`?t=`. A good one is 200 `{ok, kind, expires_at, club?, partner_app?}`;
anything else -- unknown, expired, revoked, no longer allowed, the API
switched off -- is the same 404 `{ok: false}`, with no reason.

Limited to 30 checks a minute per address. The site checks from its own
server, so, as the certificate verifier does, the visitor it names in
`X-Visitor-IP` is the one counted, behind a valid `X-Site-Token` only
(app/core/site.py); anyone else is counted by their own address.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from app.api.partner import docs_links
from app.core import site
from app.core.net import client_address
from app.core.rate_limit import limiter
from app.db.core import SessionDep

router = APIRouter(prefix="/developers", tags=["developers"])

NO_STORE = {"Cache-Control": "private, no-store"}
LIMIT = "30/minute"


def checker(request: Request) -> str:
    """Whose checks these are: the visitor the site's server names, else the caller."""
    return site.visitor_ip(request) or client_address(request) or "unknown"


@router.get("/access", include_in_schema=False)
@limiter.limit(LIMIT, key_func=checker)
async def access(request: Request, session: SessionDep, t: Optional[str] = Query(None)) -> JSONResponse:
    raw = request.headers.get(docs_links.HEADER) or t
    found = await docs_links.check(session, raw)
    if found is None:
        return JSONResponse({"ok": False}, status_code=404, headers=NO_STORE)
    await docs_links.touch(session, found.link)
    return JSONResponse(docs_links.access_out(found), headers=NO_STORE)
