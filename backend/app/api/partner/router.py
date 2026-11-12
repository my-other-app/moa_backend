"""https://api.myotherapp.in/partner/v1 -- the Integrations API.

Versioned and additive only: a breaking change is /partner/v2, run beside
v1 for six months. Every route answers in one error shape, with X-Request-Id
and the X-RateLimit headers (routing.PartnerRoute). Live credentials reach
the real objects (live.py), test credentials the sandbox (sandbox.py),
through the same routes and the same checks.

GET /partner/v1/openapi.json is this surface's OpenAPI 3.1 document: the
developer docs are rendered from it. It is private, as the docs are
(feature-register/private-dev-docs-contract.md, decision 6): it needs one of
a credential this API lets in (a club key, an app or OAuth token, live or
test), a developer docs token (`MOA-Docs-Token` or `?docs_token=`;
docs_links.py), or the website's own server (`X-Site-Token`, app/core/site.py).
Without one it is 401 UNAUTHENTICATED.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, Response

from app.api.partner import docs_links, live, sandbox
from app.api.partner import schemas as s
from app.api.partner.auth import PartnerCaller, credential_valid, partner_caller, require_scope
from app.api.partner.errors import PartnerError
from app.api.partner.routing import PartnerRoute
from app.config import settings
from app.core import site
from app.db.core import SessionDep

router = APIRouter(prefix="/partner/v1", route_class=PartnerRoute)

ERRORS = {
    401: {"model": s.ErrorBody, "description": "UNAUTHENTICATED"},
    403: {"model": s.ErrorBody, "description": "FORBIDDEN_SCOPE, NOT_ALLOCATED, CLUB_NOT_ACTIVE, APP_SUSPENDED, IP_NOT_ALLOWED"},
    404: {"model": s.ErrorBody, "description": "NOT_FOUND"},
    422: {"model": s.ErrorBody, "description": "VALIDATION, IDEMPOTENCY_MISMATCH"},
    429: {"model": s.ErrorBody, "description": "RATE_LIMITED (see Retry-After)"},
}
WRITE_ERRORS = {**ERRORS, 409: {"model": s.ErrorBody, "description": "ALREADY_EXISTS, IDEMPOTENCY_IN_PROGRESS, INVALID_STATE and the resource's own"}}
PATCH_ERRORS = {**WRITE_ERRORS, 412: {"model": s.ErrorBody, "description": "PRECONDITION_FAILED: If-Match names an older version"}}

LIMIT = Query(None, ge=1, le=100, description="At most 100; default 25.")
CURSOR = Query(None, max_length=500, description="The next_cursor of the page before.")
UPDATED_SINCE = Query(None, description="Only what changed at or after this time (ISO 8601 with offset), oldest change first.")
EXTERNAL_ID = Query(None, max_length=128, description="Find the object by your external_id.")


def ops(caller: PartnerCaller):
    return live if caller.livemode else sandbox


# ─── Who is calling ──────────────────────────────────────────────────────


@router.get("/me", response_model=s.MeOut, responses=ERRORS, tags=["auth"], summary="Who this credential is")
async def me(caller: PartnerCaller = Depends(partner_caller)):
    """The credential's type, its club (or partner), scopes, mode and rate limits."""
    return JSONResponse(live.me(caller))


# ─── Club ────────────────────────────────────────────────────────────────


@router.get("/club", response_model=s.ClubOut, responses=ERRORS, tags=["club"], summary="The club's profile")
async def get_club(caller: PartnerCaller = Depends(require_scope("club:read", "club:write"))):
    return await live.get_club(caller)


@router.patch("/club", response_model=s.ClubOut, responses=PATCH_ERRORS, tags=["club"], summary="Edit the club's profile")
async def patch_club(body: s.ClubPatch, caller: PartnerCaller = Depends(require_scope("club:write"))):
    """Name, tagline, about, links and logo (fetched from logo_url and kept). Never ownership, the team or the plan. If-Match optional."""
    if not caller.livemode:
        raise PartnerError("INVALID_STATE", "The club's profile is real in test mode too: change it with a live credential.")
    return await live.patch_club(caller, body)


@router.get("/clubs", response_model=s.ClubList, responses=ERRORS, tags=["clubs"], summary="Clubs connected to your partner app")
async def list_clubs(limit: Optional[int] = LIMIT, cursor: Optional[str] = CURSOR, caller: PartnerCaller = Depends(partner_caller)):
    """Partner apps only: every club connected to the app in this credential's mode, oldest connection first."""
    return await live.list_clubs(caller, limit=limit, cursor=cursor)


@router.post("/clubs", response_model=s.ClubOut, status_code=201, responses=WRITE_ERRORS, tags=["clubs"], summary="Create a club (pending approval)")
async def create_club(body: s.ClubCreate, caller: PartnerCaller = Depends(require_scope("clubs:create"))):
    """`clubs:create`, granted by MyOtherApp only. The club waits for the MyOtherApp team's approval, as a
    self-signup does; its owner is emailed; your app is connected to it. Once it is approved, act for it with your
    app's own token (client credentials) and the header `MOA-Club-Id: <its id>`."""
    if not caller.livemode:
        return await sandbox.create_club(caller, body)
    return await live.create_club(caller, body)


# ─── Events ──────────────────────────────────────────────────────────────


@router.get("/events", response_model=s.EventList, responses=ERRORS, tags=["events"], summary="List events")
async def list_events(
    status: Optional[str] = Query(None, pattern="^(draft|published|cancelled|ended)$"),
    starts_after: Optional[str] = Query(None), starts_before: Optional[str] = Query(None),
    updated_since: Optional[str] = UPDATED_SINCE, external_id: Optional[str] = EXTERNAL_ID,
    limit: Optional[int] = LIMIT, cursor: Optional[str] = CURSOR,
    caller: PartnerCaller = Depends(require_scope("events:read", "events:write")),
):
    return await ops(caller).list_events(
        caller, status=status, starts_after=starts_after, starts_before=starts_before, updated_since=updated_since,
        external_id=external_id, limit=limit, cursor=cursor,
    )


@router.get("/events/{event_id}", response_model=s.EventOut, responses=ERRORS, tags=["events"], summary="An event")
async def get_event(event_id: str, caller: PartnerCaller = Depends(require_scope("events:read", "events:write"))):
    return await ops(caller).get_event(caller, event_id)


@router.post("/events", response_model=s.EventOut, status_code=201, responses=WRITE_ERRORS, tags=["events"], summary="Create an event (a draft)")
async def create_event(body: s.EventCreate, caller: PartnerCaller = Depends(require_scope("events:write"))):
    """Always a draft: nobody sees it until it is published (events:publish). Idempotency-Key recommended."""
    return await ops(caller).create_event(caller, body)


@router.patch("/events/{event_id}", response_model=s.EventOut, responses=PATCH_ERRORS, tags=["events"], summary="Edit an event")
async def patch_event(event_id: str, body: s.EventPatch, caller: PartnerCaller = Depends(require_scope("events:write"))):
    """Only the fields sent change. Send If-Match with the ETag you read to be sure nobody changed it since."""
    return await ops(caller).patch_event(caller, event_id, body)


@router.post("/events/{event_id}/publish", response_model=s.EventOut, responses=WRITE_ERRORS, tags=["events"], summary="Publish a draft")
async def publish_event(event_id: str, caller: PartnerCaller = Depends(require_scope("events:publish"))):
    """A draft still to come goes live on MyOtherApp. Publishing a published event changes nothing."""
    return await ops(caller).publish_event(caller, event_id)


@router.post("/events/{event_id}/cancel", response_model=s.EventOut, responses=WRITE_ERRORS, tags=["events"], summary="Cancel an event")
async def cancel_event(event_id: str, body: s.EventCancel, caller: PartnerCaller = Depends(require_scope("events:publish"))):
    """Everyone registered is told. An event with online payments is cancelled in the dashboard, where the
    refunds are decided (409 CANCEL_IN_DASHBOARD)."""
    return await ops(caller).cancel_event(caller, event_id, body.reason)


# ─── Registrations ───────────────────────────────────────────────────────


@router.get("/events/{event_id}/registrations", response_model=s.RegistrationList, responses=ERRORS, tags=["registrations"], summary="An event's registrations")
async def list_registrations(
    event_id: str,
    status: Optional[str] = Query(None, pattern="^(confirmed|awaiting_payment|pending_approval|declined|withdrawn|cancelled)$"),
    updated_since: Optional[str] = UPDATED_SINCE, external_id: Optional[str] = EXTERNAL_ID,
    limit: Optional[int] = LIMIT, cursor: Optional[str] = CURSOR,
    caller: PartnerCaller = Depends(require_scope("registrations:read", "registrations:write")),
):
    """Oldest change first. Names, contacts and answers only with attendees:read (each such read is counted in
    the club's audit log). Registrations under review are listed with status=pending_approval."""
    return await ops(caller).list_registrations(
        caller, event_id, status=status, updated_since=updated_since, external_id=external_id, limit=limit, cursor=cursor,
    )


@router.get("/registrations/{registration_id}", response_model=s.RegistrationOut, responses=ERRORS, tags=["registrations"], summary="A registration")
async def get_registration(registration_id: str, caller: PartnerCaller = Depends(require_scope("registrations:read", "registrations:write"))):
    return await ops(caller).get_registration(caller, registration_id)


@router.post(
    "/events/{event_id}/registrations", response_model=s.RegistrationOut, status_code=201,
    responses={**WRITE_ERRORS, 202: {"model": s.RegistrationOut, "description": "Sent for review (status pending_approval)"}},
    tags=["registrations"], summary="Register someone",
)
async def create_registration(event_id: str, body: s.RegistrationCreate, caller: PartnerCaller = Depends(require_scope("registrations:write"))):
    """As the club adding a guest: the event's capacity, ticket types, answers and plan apply, and the ticket is
    emailed unless send_confirmation is false. An event that reviews registrations takes a request (202). A paid
    event answers 409 PAYMENT_REQUIRED with its checkout_url, unless the club allows payments taken outside
    MyOtherApp for it: then send `payment`. Idempotency-Key recommended."""
    return await ops(caller).create_registration(caller, event_id, body)


@router.patch("/registrations/{registration_id}", response_model=s.RegistrationOut, responses=PATCH_ERRORS, tags=["registrations"], summary="Correct a registration")
async def patch_registration(registration_id: str, body: s.RegistrationPatch, caller: PartnerCaller = Depends(require_scope("registrations:write"))):
    return await ops(caller).patch_registration(caller, registration_id, body)


@router.post("/registrations/{registration_id}/cancel", response_model=s.RegistrationOut, responses=WRITE_ERRORS, tags=["registrations"], summary="Cancel a registration")
async def cancel_registration(registration_id: str, caller: PartnerCaller = Depends(require_scope("registrations:write"))):
    """Free registrations and ones paid outside MyOtherApp only: one paid online is refunded from the dashboard
    (409 CANCEL_IN_DASHBOARD). A request under review is withdrawn."""
    return await ops(caller).cancel_registration(caller, registration_id)


# ─── Check-ins ───────────────────────────────────────────────────────────


@router.post(
    "/checkins", response_model=s.CheckinOut, status_code=201,
    responses={**WRITE_ERRORS, 200: {"model": s.CheckinOut, "description": "Already checked in: duplicate is true"}},
    tags=["checkins"], summary="Check someone in",
)
async def create_checkin(body: s.CheckinCreate, caller: PartnerCaller = Depends(require_scope("checkins:write"))):
    """By ticket_code, registration_id or your external_id. One check-in per person: a second answers 200 with
    duplicate: true."""
    return await ops(caller).create_checkin(caller, body)


@router.delete("/checkins/{checkin_id}", response_model=s.CheckinOut, responses=WRITE_ERRORS, tags=["checkins"], summary="Undo a check-in")
async def delete_checkin(checkin_id: str, caller: PartnerCaller = Depends(require_scope("checkins:write"))):
    return await ops(caller).delete_checkin(caller, checkin_id)


@router.get("/events/{event_id}/checkins", response_model=s.CheckinList, responses=ERRORS, tags=["checkins"], summary="An event's check-ins")
async def list_checkins(
    event_id: str, updated_since: Optional[str] = UPDATED_SINCE, limit: Optional[int] = LIMIT, cursor: Optional[str] = CURSOR,
    caller: PartnerCaller = Depends(require_scope("registrations:read", "checkins:write")),
):
    return await ops(caller).list_checkins(caller, event_id, updated_since=updated_since, limit=limit, cursor=cursor)


# ─── Certificates ────────────────────────────────────────────────────────


@router.get("/events/{event_id}/certificates", response_model=s.CertificateList, responses=ERRORS, tags=["certificates"], summary="An event's certificates")
async def list_certificates(
    event_id: str, limit: Optional[int] = LIMIT, cursor: Optional[str] = CURSOR,
    caller: PartnerCaller = Depends(require_scope("certificates:read", "certificates:write")),
):
    return await ops(caller).list_certificates(caller, event_id, limit=limit, cursor=cursor)


@router.post("/events/{event_id}/certificates", response_model=s.CertificateJobOut, status_code=202, responses=WRITE_ERRORS, tags=["certificates"], summary="Issue certificates")
async def issue_certificates(event_id: str, body: s.CertificateIssue, caller: PartnerCaller = Depends(require_scope("certificates:write"))):
    """To registration_ids, or everyone checked in. Nobody is issued twice. Answers with the job."""
    return await ops(caller).issue_certificates(caller, event_id, body)


@router.get("/certificate-jobs/{job_id}", response_model=s.CertificateJobOut, responses=ERRORS, tags=["certificates"], summary="A certificate job")
async def get_certificate_job(job_id: str, caller: PartnerCaller = Depends(require_scope("certificates:read", "certificates:write"))):
    return await ops(caller).get_certificate_job(caller, job_id)


@router.get("/certificates/{number}", response_model=s.CertificateVerification, responses=ERRORS, tags=["certificates"], summary="Verify a certificate")
async def get_certificate(number: str, caller: PartnerCaller = Depends(require_scope("certificates:read", "certificates:write"))):
    """One of the club's certificates by its number: whether it is valid, and its PDF."""
    return await ops(caller).get_certificate(caller, number)


# ─── Tickets' QR codes ───────────────────────────────────────────────────


@router.get("/tickets/{code}/qr.png", include_in_schema=False)
async def ticket_qr(code: str, request: Request, sig: str = Query(..., max_length=600)):
    """A ticket's QR, behind the signed link a registration carries (a week)."""
    import asyncio
    import base64

    from app.api.events.documents import qr_png_base64
    from app.core.auth.payloads import verify_payload

    payload = verify_payload(sig)
    if not payload or payload.get("k") != "partner_qr" or payload.get("t") != code:
        raise PartnerError("NOT_FOUND", "This QR link is not valid or has expired.")
    png = await asyncio.to_thread(qr_png_base64, code)
    return Response(content=base64.b64decode(png.split(",", 1)[-1]), media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})


# ─── The document ────────────────────────────────────────────────────────


#: Every way in to the document varies the answer: no shared cache may keep it.
SPEC_HEADERS = {
    "Cache-Control": "private, no-store",
    "Vary": f"Authorization, {docs_links.HEADER}, X-Site-Token",
    "Access-Control-Allow-Origin": "*",
}


async def spec_reader(session, request: Request) -> Optional[str]:
    """Who may read the document: "site", "partner" or "docs"; None for anyone else."""
    if site.token_id(request) is not None:
        return "site"
    if await credential_valid(session, request):
        return "partner"
    found = await docs_links.check(session, docs_links.token_of(request))
    if found is not None:
        await docs_links.touch(session, found.link)
        return "docs"
    return None


@router.get("/openapi.json", include_in_schema=False)
async def openapi_document(request: Request, session: SessionDep):
    from app.api.partner.openapi import document

    if not settings.PARTNER_API_ENABLED:
        raise PartnerError("NOT_FOUND", "Not found.")
    if await spec_reader(session, request) is None:
        raise PartnerError(
            "UNAUTHENTICATED",
            f"The API reference is private. Send an API key or access token (Authorization: Bearer <it>), or a developer docs token ({docs_links.HEADER}).",
            headers={"WWW-Authenticate": 'Bearer realm="partner"', "Cache-Control": "private, no-store"},
        )
    return JSONResponse(document(), headers=SPEC_HEADERS)


# WEBHOOKS_ROUTER_MOUNT
# The webhook endpoints (app/api/webhooks_out/partner.py) mount here, as PartnerRoutes so they answer in this
# API's shape with its headers and idempotency.
from app.api.partner.routing import include_partner_routes  # noqa: E402
from app.api.webhooks_out.partner import make_router as _webhooks_router  # noqa: E402

include_partner_routes(router, _webhooks_router(require_scope), tags=["webhooks"])
