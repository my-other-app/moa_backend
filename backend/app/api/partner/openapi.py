"""GET /partner/v1/openapi.json: the OpenAPI 3.1 document for exactly the
Integrations API (no internal route is in it), with the bodies' and the
answers' examples. The developer docs (myotherapp.in/developers) render it;
it is built once per process from the routes themselves, so a route added
to the partner router -- the webhook endpoints included, once mounted --
is in it, with the scopes its dependency asks for (`x-scopes`).

It is private, as the docs' pages are: router.py's `spec_reader` lets in a
partner credential, a developer docs token or the website's own server, and
answers anyone else 401 (feature-register/private-dev-docs-contract.md).
"""
from __future__ import annotations

import copy
from typing import Optional

from fastapi.openapi.utils import get_openapi

from app.api.partner import scopes as catalogue
from app.config import settings

VERSION = "1.0.0"
_cached: Optional[dict] = None

DESCRIPTION = """\
The MyOtherApp Integrations API lets a club's own systems, and platforms working with many clubs, work with
MyOtherApp both ways: create and publish events, register people, check them in and issue certificates -- and,
with webhooks, hear when any of that happens on MyOtherApp.

**Access.** Nothing is on by default: the MyOtherApp team turns API access on for a club. Then the club's owner
or an admin makes keys in Club dashboard -> Settings -> Integrations. Partner platforms are registered by the
MyOtherApp team and connected by each club over OAuth 2.0 (authorization code with PKCE, refresh tokens).

**Requests.** `Authorization: Bearer <key or access token>`. Bodies are JSON. Times are ISO 8601 with their offset;
money is whole paise with `currency: "INR"`; ids are strings. Lists are `{data, next_cursor}` (`limit` up to 100,
default 25; pass `cursor` for the next page). `updated_since` gives what changed since, oldest change first.

**Safety.** Send `Idempotency-Key` on every POST and DELETE: a retry returns the first answer (24 hours); the same
key with another request is 422 IDEMPOTENCY_MISMATCH. Every object has an `ETag`; PATCH with `If-Match` is 412
PRECONDITION_FAILED when someone changed it since you read it. `external_id` (your own id, unique per credential
and resource) finds an object, and a second POST with it is 409 ALREADY_EXISTS with the existing id.

**Errors.** `{"error": {"code", "message", "details", "request_id"}}`; every answer has `X-Request-Id` and
`X-RateLimit-Limit/Remaining/Reset`; 429 has `Retry-After`.

**Test mode.** `moa_test_` keys (and partner test tokens, `mode=test`) work on a sandbox: test objects only
(`livemode: false`), never public, no emails, WhatsApp or push, no payments. Live credentials never see them.

**Money and personal data.** Money never moves through the API. A paid event refuses a registration
(PAYMENT_REQUIRED, with its checkout_url) unless the club allows payments taken outside MyOtherApp for that event.
Attendees' names, contacts and answers need `attendees:read`, which the club grants having told people their
details are shared (India's DPDP Act 2023); every read of them is counted in the club's audit log.
"""

TAGS = [
    {"name": "auth", "description": "Who a credential is."},
    {"name": "club", "description": "The club's own profile."},
    {"name": "clubs", "description": "Partner apps: connected clubs, and creating clubs (clubs:create)."},
    {"name": "events", "description": "Events: drafts, publishing, cancelling."},
    {"name": "registrations", "description": "Registrations: adding, correcting, cancelling."},
    {"name": "checkins", "description": "Check-ins at the door."},
    {"name": "certificates", "description": "Issuing and verifying certificates."},
    {"name": "webhooks", "description": "Webhook endpoints: being told of changes."},
]

_HEADERS = {
    "X-Request-Id": {"description": "This request's id: quote it to MyOtherApp.", "schema": {"type": "string"}},
    "X-RateLimit-Limit": {"description": "Requests of this kind (reads or writes) allowed a minute.", "schema": {"type": "integer"}},
    "X-RateLimit-Remaining": {"description": "Left in this minute.", "schema": {"type": "integer"}},
    "X-RateLimit-Reset": {"description": "When the minute resets (unix seconds).", "schema": {"type": "integer"}},
}


def _scopes_of(route) -> list[str]:
    found: list[str] = []

    def walk(dependant):
        for dep in dependant.dependencies:
            for scope in getattr(dep.call, "partner_scopes", ()) or ():
                if scope not in found:
                    found.append(scope)
            walk(dep)

    walk(route.dependant)
    return found


def build() -> dict:
    from app.api.partner.oauth import endpoints
    from app.api.partner.router import router

    routes = [r for r in router.routes if getattr(r, "include_in_schema", True)]
    doc = get_openapi(
        title="MyOtherApp Integrations API", version=VERSION, openapi_version="3.1.0", description=DESCRIPTION,
        routes=routes, tags=TAGS, servers=[{"url": settings.PUBLIC_API_URL.rstrip("/"), "description": "Live and test (the credential decides)"}],
        contact={"name": "MyOtherApp", "url": settings.PUBLIC_SITE_URL.rstrip("/") + "/developers"},
    )
    components = doc.setdefault("components", {})
    schemas = components.setdefault("schemas", {})
    urls = endpoints()
    components["securitySchemes"] = {
        "apiKey": {
            "type": "http", "scheme": "bearer", "bearerFormat": "moa_live_... / moa_test_...",
            "description": "A club's own API key (Club dashboard -> Settings -> Integrations).",
        },
        "partnerOAuth": {
            "type": "oauth2",
            "description": "Partner apps registered by MyOtherApp. PKCE (S256) is required; add mode=test for a test connection.",
            "flows": {
                "authorizationCode": {
                    "authorizationUrl": urls["authorization_endpoint"], "tokenUrl": urls["token_endpoint"],
                    "refreshUrl": urls["token_endpoint"],
                    "scopes": {k: v[0] for k, v in catalogue.SCOPES.items() if k != "clubs:create"},
                },
                "clientCredentials": {"tokenUrl": urls["token_endpoint"], "scopes": {"clubs:create": catalogue.SCOPES["clubs:create"][0]}},
            },
        },
    }
    doc["security"] = [{"apiKey": []}, {"partnerOAuth": []}]
    by_path = {}
    for route in routes:
        for method in route.methods or ():
            by_path[(route.path_format, method.lower())] = route
    for path, item in doc.get("paths", {}).items():
        for method, op in item.items():
            route = by_path.get((path, method))
            scopes = _scopes_of(route) if route is not None else []
            op["x-scopes"] = scopes
            if scopes:
                op["description"] = (op.get("description") or "").rstrip() + f"\n\nNeeds one of: {', '.join(f'`{x}`' for x in scopes)}."
                op["security"] = [{"apiKey": []}, {"partnerOAuth": scopes}]
            params = op.setdefault("parameters", [])
            if not path.endswith("/clubs"):
                params.append({"name": "MOA-Club-Id", "in": "header", "required": False, "schema": {"type": "string"},
                               "description": "With a partner app's own token (client credentials): the connected club to act for."})
            if method in ("post", "delete"):
                params.append({"name": "Idempotency-Key", "in": "header", "required": False, "schema": {"type": "string", "maxLength": 255},
                               "description": "Any unique string: a retry with it returns the first answer (24 hours)."})
            if method == "patch":
                params.append({"name": "If-Match", "in": "header", "required": False, "schema": {"type": "string"},
                               "description": "The ETag you read: 412 PRECONDITION_FAILED when the object changed since."})
            for status, response in op.get("responses", {}).items():
                response.setdefault("headers", {}).update(copy.deepcopy(_HEADERS))
                if status == "429":
                    response["headers"]["Retry-After"] = {"description": "Seconds to wait.", "schema": {"type": "integer"}}
                content = response.get("content", {}).get("application/json", {})
                ref = (content.get("schema") or {}).get("$ref", "")
                if ref.endswith("/HTTPValidationError"):
                    content["schema"] = {"$ref": "#/components/schemas/ErrorBody"}
                    response["description"] = "VALIDATION"
    for unused in ("HTTPValidationError", "ValidationError"):
        if unused in schemas and unused not in str(doc.get("paths")):
            schemas.pop(unused, None)
    # The webhooks themselves (OpenAPI 3.1 `webhooks`): each type's body and headers (app/api/webhooks_out/openapi.py).
    from app.api.webhooks_out.openapi import add_to as add_webhooks

    add_webhooks(doc)
    doc["info"]["x-changelog"] = [{"version": VERSION, "date": "2026-11-12", "changes": "The first version."}]
    doc["info"]["x-oauth"] = {**urls, "scopes": catalogue.catalogue()}
    return doc


def document() -> dict:
    global _cached
    if _cached is None:
        _cached = build()
    return _cached


def reset() -> None:
    global _cached
    _cached = None
