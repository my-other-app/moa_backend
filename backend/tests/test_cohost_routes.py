"""Every club-side event route, and what a co-host may do there (docs/COHOSTS.md).

A co-host's team may do on an event everything the host's team may, except
the host's own decisions (app/api/cohosts/policy.py). This file holds the
route table: every route a club can call whose path names an event, each
classified --

- ALLOW: a co-host is treated exactly as the host;
- HOST_ONLY: a co-host is refused, 403 COHOST_HOST_ONLY;
- COHOST_ONLY: only a co-host may (leaving);
- PUBLIC: not a club's own route (anyone may call it), compared only.

`test_every_club_side_event_route_is_in_the_table` fails for a route added
later until it is classified here; `test_a_route_treats_a_cohost_as_the_host`
then calls each one three times -- as a stranger club on one event, as the
co-host on that event, as the host on a twin of it -- with path, query and
body made from the OpenAPI schema, and asserts the co-host's answer is the
host's (same status, error code and message), the stranger's a refusal, and
a host-only route's a 403 COHOST_HOST_ONLY for the co-host.

A route that finds its event another way (a ticket id, a body field) is
covered in tests/test_cohosts.py.
"""
import copy
import re
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest

from app.api.closeout.models import EventStatus
from app.api.cohosts import policy
from app.api.cohosts.models import ACCEPTED, EventCohosts
from app.core.rate_limit import limiter
from tests.conftest import auth_header

ALLOW, HOST_ONLY, COHOST_ONLY, PUBLIC = "allow", "host_only", "cohost_only", "public"

ROUTES = {
    # cohosts
    "GET /api/v1/events/{event_id}/cohosts": ALLOW,
    "GET /api/v1/events/{event_id}/cohosts/search": HOST_ONLY,
    "POST /api/v1/events/{event_id}/cohosts": HOST_ONLY,
    "POST /api/v1/events/{event_id}/cohosts/leave": COHOST_ONLY,
    "DELETE /api/v1/events/{event_id}/cohosts/{invite_id}": HOST_ONLY,
    # analytics
    "GET /api/v1/events/{event_id}/figures": ALLOW,
    "GET /api/v1/events/{event_id}/summary": ALLOW,
    # links
    "GET /api/v1/events/{event_id}/links": ALLOW,
    "GET /api/v1/events/{event_id}/links/overview": ALLOW,
    "GET /api/v1/events/{event_id}/links/{link_id}/detail": ALLOW,
    "POST /api/v1/events/{event_id}/links": ALLOW,
    "PATCH /api/v1/events/{event_id}/links/{link_id}": ALLOW,
    "DELETE /api/v1/events/{event_id}/links/{link_id}": ALLOW,
    # finance
    "GET /api/v1/events/{event_id}/finance": ALLOW,
    "GET /api/v1/events/{event_id}/finance/statement.csv": ALLOW,
    # live
    "GET /api/v1/events/{event_id}/live/quiz": ALLOW,
    "PATCH /api/v1/events/{event_id}/live/quiz": ALLOW,
    "POST /api/v1/events/{event_id}/live/quiz/questions": ALLOW,
    "PATCH /api/v1/events/{event_id}/live/quiz/questions/{question_id}": ALLOW,
    "DELETE /api/v1/events/{event_id}/live/quiz/questions/{question_id}": ALLOW,
    "GET /api/v1/events/{event_id}/live/quiz/host": ALLOW,
    "POST /api/v1/events/{event_id}/live/quiz/host/{action}": ALLOW,
    "GET /api/v1/events/{event_id}/live/questions": ALLOW,
    "PATCH /api/v1/events/{event_id}/live/questions/settings": ALLOW,
    "POST /api/v1/events/{event_id}/live/questions/{question_id}": ALLOW,
    "GET /api/v1/events/{event_id}/live/polls": ALLOW,
    "POST /api/v1/events/{event_id}/live/polls": ALLOW,
    "PATCH /api/v1/events/{event_id}/live/polls/{poll_id}": ALLOW,
    "POST /api/v1/events/{event_id}/live/polls/{poll_id}/status": ALLOW,
    "DELETE /api/v1/events/{event_id}/live/polls/{poll_id}": ALLOW,
    "GET /api/v1/events/{event_id}/live/feedback": ALLOW,
    "PUT /api/v1/events/{event_id}/live/feedback": ALLOW,
    "POST /api/v1/events/{event_id}/live/feedback/send": ALLOW,
    "GET /api/v1/events/{event_id}/live/updates": ALLOW,
    "POST /api/v1/events/{event_id}/live/updates": ALLOW,
    "POST /api/v1/events/{event_id}/live/schedule/shift": ALLOW,
    "GET /api/v1/events/{event_id}/live/screen": ALLOW,
    "POST /api/v1/events/{event_id}/live/questions": PUBLIC,
    "POST /api/v1/events/{event_id}/live/questions/{question_id}/vote": PUBLIC,
    "POST /api/v1/events/{event_id}/live/polls/{poll_id}/respond": PUBLIC,
    # competitions
    "GET /api/v1/events/{event_id}/competition": ALLOW,
    "PATCH /api/v1/events/{event_id}/competition": ALLOW,
    "POST /api/v1/events/{event_id}/competition/tracks": ALLOW,
    "PATCH /api/v1/events/{event_id}/competition/tracks/{track_id}": ALLOW,
    "DELETE /api/v1/events/{event_id}/competition/tracks/{track_id}": ALLOW,
    "POST /api/v1/events/{event_id}/competition/teams": ALLOW,
    "PATCH /api/v1/events/{event_id}/competition/teams/{team_id}": ALLOW,
    "DELETE /api/v1/events/{event_id}/competition/teams/{team_id}": ALLOW,
    "POST /api/v1/events/{event_id}/competition/teams/{team_id}/members": ALLOW,
    "DELETE /api/v1/events/{event_id}/competition/teams/{team_id}/members/{registration_id}": ALLOW,
    "POST /api/v1/events/{event_id}/competition/submissions/{action}": ALLOW,
    "POST /api/v1/events/{event_id}/competition/judges": ALLOW,
    "PATCH /api/v1/events/{event_id}/competition/judges/{judge_id}": ALLOW,
    "DELETE /api/v1/events/{event_id}/competition/judges/{judge_id}": ALLOW,
    "PUT /api/v1/events/{event_id}/competition/judges/{judge_id}/scores/{team_id}": ALLOW,
    "POST /api/v1/events/{event_id}/competition/rounds/advance": ALLOW,
    "POST /api/v1/events/{event_id}/competition/mentors": ALLOW,
    "PATCH /api/v1/events/{event_id}/competition/mentors/{mentor_id}": ALLOW,
    "DELETE /api/v1/events/{event_id}/competition/mentors/{mentor_id}": ALLOW,
    "POST /api/v1/events/{event_id}/competition/mentors/{mentor_id}/slots": ALLOW,
    "PATCH /api/v1/events/{event_id}/competition/slots/{slot_id}": ALLOW,
    "DELETE /api/v1/events/{event_id}/competition/slots/{slot_id}": ALLOW,
    # ai
    "GET /api/v1/events/{event_id}/ai/feedback-summary": ALLOW,
    "POST /api/v1/events/{event_id}/ai/feedback-summary/refresh": ALLOW,
    # store
    "GET /api/v1/events/{event_id}/store": ALLOW,
    "GET /api/v1/events/{event_id}/store/offers": ALLOW,
    "PUT /api/v1/events/{event_id}/store/offers": ALLOW,
    "GET /api/v1/events/{event_id}/store/summary": ALLOW,
    "PATCH /api/v1/events/{event_id}/store/settings": ALLOW,
    "POST /api/v1/events/{event_id}/store": ALLOW,
    "POST /api/v1/events/{event_id}/store/photo": ALLOW,
    "PATCH /api/v1/events/{event_id}/store/{item_id}": ALLOW,
    "DELETE /api/v1/events/{event_id}/store/{item_id}": ALLOW,
    "GET /api/v1/events/{event_id}/store/orders": ALLOW,
    "POST /api/v1/events/{event_id}/store/orders/{order_id}/handover": ALLOW,
    "POST /api/v1/events/{event_id}/store/orders/{order_id}/fulfil": ALLOW,
    "POST /api/v1/events/{event_id}/store/orders/{order_id}/handover/undo": ALLOW,
    "GET /api/v1/events/{event_id}/gate/store/{registration_id}": ALLOW,
    "POST /api/v1/events/{event_id}/gate/store/{order_id}/handover": ALLOW,
    # ticketing
    "GET /api/v1/events/{event_id}/ticketing/types": ALLOW,
    "PUT /api/v1/events/{event_id}/ticketing/types": ALLOW,
    "POST /api/v1/events/{event_id}/ticketing/types": ALLOW,
    "PATCH /api/v1/events/{event_id}/ticketing/types/{type_id}": ALLOW,
    "DELETE /api/v1/events/{event_id}/ticketing/types/{type_id}": ALLOW,
    "PUT /api/v1/events/{event_id}/ticketing/types/order": ALLOW,
    "GET /api/v1/events/{event_id}/ticketing/sales": ALLOW,
    "GET /api/v1/events/{event_id}/ticketing/codes": ALLOW,
    "GET /api/v1/events/{event_id}/ticketing/waitlist": ALLOW,
    "PUT /api/v1/events/{event_id}/ticketing/waitlist/settings": ALLOW,
    "POST /api/v1/events/{event_id}/ticketing/waitlist/{entry_id}/offer": ALLOW,
    "DELETE /api/v1/events/{event_id}/ticketing/waitlist/{entry_id}": ALLOW,
    "GET /api/v1/events/{event_id}/ticketing/waitlist/export": ALLOW,
    # approvals
    "GET /api/v1/events/registration/{event_id}/requests": ALLOW,
    "GET /api/v1/events/registration/{event_id}/requests/export": ALLOW,
    "POST /api/v1/events/registration/{event_id}/requests/approve": ALLOW,
    "POST /api/v1/events/registration/{event_id}/requests/decline": ALLOW,
    "POST /api/v1/events/registration/{event_id}/requests/reopen": ALLOW,
    # clubs
    "POST /api/v1/clubs/notifications/events/{event_id}/send": ALLOW,
    "GET /api/v1/clubs/notifications/events/{event_id}/stats": ALLOW,
    "GET /api/v1/clubs/notifications/events/{event_id}/audience": ALLOW,
    # event_designs
    "GET /api/v1/events/{event_id}/designs/{kind}": ALLOW,
    "PUT /api/v1/events/{event_id}/designs/{kind}": ALLOW,
    "DELETE /api/v1/events/{event_id}/designs/{kind}": ALLOW,
    "PUT /api/v1/events/{event_id}/designs/{kind}/artwork": ALLOW,
    "GET /api/v1/events/{event_id}/designs/{kind}/sample": ALLOW,
    "POST /api/v1/events/{event_id}/designs/{kind}/preview": ALLOW,
    "GET /api/v1/events/{event_id}/designs/{kind}/layout": ALLOW,
    "GET /api/v1/events/{event_id}/designs/{kind}/layout/starters/{name}": ALLOW,
    "PUT /api/v1/events/{event_id}/designs/{kind}/layout": ALLOW,
    "POST /api/v1/events/{event_id}/designs/{kind}/layout/preview": ALLOW,
    "POST /api/v1/events/{event_id}/designs/{kind}/layout/images": ALLOW,
    # deliveries
    "POST /api/v1/events/{event_id}/tickets/send": ALLOW,
    "POST /api/v1/events/{event_id}/tickets/{registration_id}/send": ALLOW,
    "POST /api/v1/events/{event_id}/tickets/{registration_id}/revoke": ALLOW,
    "POST /api/v1/events/{event_id}/tickets/{registration_id}/reinstate": ALLOW,
    "GET /api/v1/events/{event_id}/tickets/bundle.zip": ALLOW,
    "GET /api/v1/events/{event_id}/deliveries/summary": ALLOW,
    "GET /api/v1/events/{event_id}/deliveries": ALLOW,
    # certificates
    "GET /api/v1/events/{event_id}/certificates/summary": ALLOW,
    "POST /api/v1/events/{event_id}/certificates/issue-preview": ALLOW,
    "POST /api/v1/events/{event_id}/certificates/issue": ALLOW,
    "POST /api/v1/events/{event_id}/certificates/send": ALLOW,
    "POST /api/v1/events/{event_id}/certificates/{registration_id}/send": ALLOW,
    "POST /api/v1/events/{event_id}/certificates/{registration_id}/revoke": ALLOW,
    "POST /api/v1/events/{event_id}/certificates/{registration_id}/reinstate": ALLOW,
    "GET /api/v1/events/{event_id}/certificates/bundle.zip": ALLOW,
    "GET /api/v1/events/{event_id}/certificates": ALLOW,
    # emails
    "GET /api/v1/events/{event_id}/emails": ALLOW,
    "GET /api/v1/events/{event_id}/emails/context": ALLOW,
    "POST /api/v1/events/{event_id}/emails": ALLOW,
    "POST /api/v1/events/{event_id}/emails/preview": ALLOW,
    "POST /api/v1/events/{event_id}/emails/test": ALLOW,
    "POST /api/v1/events/{event_id}/emails/audience": ALLOW,
    "POST /api/v1/events/{event_id}/emails/images": ALLOW,
    "GET /api/v1/events/{event_id}/emails/{template_id}": ALLOW,
    "PUT /api/v1/events/{event_id}/emails/{template_id}": ALLOW,
    "DELETE /api/v1/events/{event_id}/emails/{template_id}": ALLOW,
    "POST /api/v1/events/{event_id}/emails/{template_id}/duplicate": ALLOW,
    "POST /api/v1/events/{event_id}/emails/{template_id}/save-to-library": ALLOW,
    "POST /api/v1/events/{event_id}/emails/{template_id}/reset": ALLOW,
    "POST /api/v1/events/{event_id}/emails/{template_id}/send": ALLOW,
    # checkins
    "GET /api/v1/events/{event_id}/doors": ALLOW,
    "POST /api/v1/events/{event_id}/doors": ALLOW,
    "PATCH /api/v1/events/{event_id}/doors/{door_id}": ALLOW,
    "DELETE /api/v1/events/{event_id}/doors/{door_id}": ALLOW,
    "POST /api/v1/events/{event_id}/doors/{door_id}/link": ALLOW,
    "DELETE /api/v1/events/{event_id}/doors/{door_id}/link": ALLOW,
    "GET /api/v1/events/{event_id}/team": ALLOW,
    "PATCH /api/v1/events/{event_id}/team/{volunteer_id}": ALLOW,
    "PATCH /api/v1/events/{event_id}/gate-settings": ALLOW,
    "GET /api/v1/events/{event_id}/gate/context": ALLOW,
    "POST /api/v1/events/{event_id}/gate/scan": ALLOW,
    "POST /api/v1/events/{event_id}/gate/scan-batch": ALLOW,
    "GET /api/v1/events/{event_id}/gate/search": ALLOW,
    "POST /api/v1/events/{event_id}/gate/check-in/{registration_id}": ALLOW,
    "DELETE /api/v1/events/{event_id}/gate/check-in/{registration_id}": ALLOW,
    "POST /api/v1/events/{event_id}/gate/walkup": ALLOW,
    "POST /api/v1/events/{event_id}/gate/capture/{registration_id}": ALLOW,
    "GET /api/v1/events/{event_id}/check-ins": ALLOW,
    # idcards
    "GET /api/v1/events/{event_id}/idcards/summary": ALLOW,
    "PATCH /api/v1/events/{event_id}/idcards/settings": ALLOW,
    "GET /api/v1/events/{event_id}/idcards/jobs": ALLOW,
    "GET /api/v1/events/{event_id}/idcards/batch.pdf": ALLOW,
    "GET /api/v1/events/{event_id}/idcards/{registration_id}.pdf": ALLOW,
    # kiosk
    "GET /api/v1/events/{event_id}/kiosks": ALLOW,
    "POST /api/v1/events/{event_id}/kiosks": ALLOW,
    "GET /api/v1/events/{event_id}/kiosks/settings": ALLOW,
    "PATCH /api/v1/events/{event_id}/kiosks/settings": ALLOW,
    "POST /api/v1/events/{event_id}/kiosks/{device_id}/rotate": ALLOW,
    "DELETE /api/v1/events/{event_id}/kiosks/{device_id}": ALLOW,
    # agenda
    "POST /api/v1/events/{event_id}/agenda": ALLOW,
    "PATCH /api/v1/events/{event_id}/agenda/{item_id}": ALLOW,
    "DELETE /api/v1/events/{event_id}/agenda/{item_id}": ALLOW,
    "GET /api/v1/events/{event_id}/agenda/programme.pdf": ALLOW,
    # organisers
    "GET /api/v1/events/{event_id}/organisers": ALLOW,
    "PUT /api/v1/events/{event_id}/organisers": ALLOW,
    # event_partners
    "GET /api/v1/events/{event_id}/partners": ALLOW,
    "POST /api/v1/events/{event_id}/partners": ALLOW,
    "PUT /api/v1/events/{event_id}/partners/order": ALLOW,
    "POST /api/v1/events/{event_id}/partners/from-club": ALLOW,
    "PATCH /api/v1/events/{event_id}/partners/{partner_id}": ALLOW,
    "PUT /api/v1/events/{event_id}/partners/{partner_id}/logo": ALLOW,
    "DELETE /api/v1/events/{event_id}/partners/{partner_id}": ALLOW,
    # merch
    "GET /api/v1/events/{event_id}/merch": ALLOW,
    "GET /api/v1/events/{event_id}/merch/fields": ALLOW,
    "POST /api/v1/events/{event_id}/merch": ALLOW,
    "POST /api/v1/events/{event_id}/merch/photo": ALLOW,
    "GET /api/v1/events/{event_id}/merch/collections": ALLOW,
    "GET /api/v1/events/{event_id}/merch/analytics": ALLOW,
    "PATCH /api/v1/events/{event_id}/merch/{item_id}": ALLOW,
    "DELETE /api/v1/events/{event_id}/merch/{item_id}": ALLOW,
    "POST /api/v1/events/{event_id}/merch/{item_id}/handout": ALLOW,
    "POST /api/v1/events/{event_id}/merch/{item_id}/handout/undo": ALLOW,
    "POST /api/v1/events/{event_id}/merch/{item_id}/variants": ALLOW,
    "PATCH /api/v1/events/{event_id}/merch/{item_id}/variants/{variant_id}": ALLOW,
    "DELETE /api/v1/events/{event_id}/merch/{item_id}/variants/{variant_id}": ALLOW,
    "GET /api/v1/events/{event_id}/gate/merch/{registration_id}": ALLOW,
    "POST /api/v1/events/{event_id}/gate/merch/{item_id}/collect": ALLOW,
    # closeout
    "GET /api/v1/events/{event_id}/closeout/figures": ALLOW,
    "GET /api/v1/events/{event_id}/closeout/reports": ALLOW,
    "POST /api/v1/events/{event_id}/closeout/reports": ALLOW,
    "GET /api/v1/events/{event_id}/closeout/reports/{report_id}.pdf": ALLOW,
    "GET /api/v1/events/{event_id}/closeout/settings": ALLOW,
    "PATCH /api/v1/events/{event_id}/closeout/settings": ALLOW,
    "GET /api/v1/events/{event_id}/closeout/retention": ALLOW,
    "POST /api/v1/events/{event_id}/closeout/retention/run": HOST_ONLY,
    # club_ops
    "GET /api/v1/events/{event_id}/results": ALLOW,
    "PUT /api/v1/events/{event_id}/results/teams/{team_id}": ALLOW,
    "GET /api/v1/events/{event_id}/payments": ALLOW,
    "POST /api/v1/events/{event_id}/payments/{payment_id}/refund": HOST_ONLY,
    "POST /api/v1/events/{event_id}/refunds/{refund_id}/retry": HOST_ONLY,
    "GET /api/v1/events/{event_id}/cancel": ALLOW,
    "POST /api/v1/events/{event_id}/cancel": HOST_ONLY,
    "POST /api/v1/events/{event_id}/cancel/retry": HOST_ONLY,
    # events
    "POST /api/v1/events/{event_id}/duplicate": HOST_ONLY,
    # admission
    "GET /api/v1/events/{event_id}/admission": ALLOW,
    "PATCH /api/v1/events/{event_id}/admission": ALLOW,
    "POST /api/v1/events/{event_id}/admission/number-all": ALLOW,
    # emails
    "GET /api/v1/events/{event_id}/messages": ALLOW,
    "POST /api/v1/events/{event_id}/messages": ALLOW,
    "GET /api/v1/events/{event_id}/messages/{message_id}": ALLOW,
    # events
    "GET /api/v1/events/volunteer/list/{event_id}": ALLOW,
    "POST /api/v1/events/volunteer/checkin/{event_id}": ALLOW,
    "POST /api/v1/events/volunteer/validate/{event_id}": ALLOW,
    "GET /api/v1/events/registration/{event_id}/list": ALLOW,
    "GET /api/v1/events/registration/{event_id}/attendees": ALLOW,
    "GET /api/v1/events/registration/{event_id}/analytics": ALLOW,
    "GET /api/v1/events/registration/{event_id}/export": ALLOW,
    "GET /api/v1/events/registration/{event_id}/info/{registration_id}": ALLOW,
    "PATCH /api/v1/events/registration/{event_id}/info/{registration_id}": ALLOW,
    "DELETE /api/v1/events/registration/{event_id}/info/{registration_id}": ALLOW,
    "POST /api/v1/events/registration/{event_id}/attendees": ALLOW,
    "POST /api/v1/events/registration/{event_id}/import": ALLOW,
    "GET /api/v1/events/registration/{event_id}/imports": ALLOW,
    "POST /api/v1/events/registration/{event_id}/attendance/{registration_id}": ALLOW,
    "GET /api/v1/events/registration/{event_id}/winners": ALLOW,
    "PUT /api/v1/events/registration/{event_id}/winners/{registration_id}": ALLOW,
    "PUT /api/v1/events/registration/{event_id}/role/{registration_id}": ALLOW,
    "PUT /api/v1/events/update/{event_id}": ALLOW,
    "GET /api/v1/events/info/{event_id}": PUBLIC,
    "DELETE /api/v1/events/delete/{event_id}": HOST_ONLY,
    "GET /api/v1/events/ratings/{event_id}": ALLOW,
    "GET /api/v1/events/count-view/{event_id}": PUBLIC,
    "GET /api/v1/events/{event_id}/networking": PUBLIC,
}


# ─── The app's routes ───────────────────────────────────────────────────────


def _walk(routes):
    from fastapi.routing import _IncludedRouter

    for r in routes:
        if isinstance(r, _IncludedRouter):
            yield from _walk(r.effective_candidates())
        else:
            yield r


def _roles(dependant, into: set) -> set:
    for d in dependant.dependencies:
        if "role_checker" in getattr(d.call, "__qualname__", ""):
            for cell in d.call.__closure__ or ():
                if isinstance(cell.cell_contents, list):
                    into.update(cell.cell_contents)
        _roles(d, into)
    return into


def club_side_event_routes() -> set[str]:
    """"METHOD path" for every route a club account may call whose path names an event."""
    from doth import application

    found = set()
    for ctx in _walk(application.routes):
        route = getattr(ctx, "original_route", None)
        path = getattr(ctx, "path", "")
        if route is None or not hasattr(route, "dependant") or "{event_id}" not in path:
            continue
        if "club" not in _roles(route.dependant, set()):
            continue
        for method in route.methods - {"HEAD"}:
            found.add(f"{method} {path}")
    return found


# ─── Requests made from the schema ──────────────────────────────────────────


_spec_cache: dict = {}


def _spec() -> dict:
    if not _spec_cache:
        from doth import application

        _spec_cache.update(application.openapi())
    return _spec_cache


def _resolve(schema: dict) -> dict:
    while "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        schema = _spec()["components"]["schemas"][name]
    return schema


def sample(schema: dict, depth: int = 0):
    """A small valid value for a JSON schema: required fields only."""
    schema = _resolve(schema or {})
    if depth > 6:
        return None
    if "allOf" in schema:
        merged = {}
        for part in schema["allOf"]:
            merged.update(_resolve(part))
        return sample(merged, depth + 1)
    for key in ("anyOf", "oneOf"):
        if key in schema:
            options = [o for o in schema[key] if _resolve(o).get("type") != "null"]
            return sample(options[0], depth + 1) if options else None
    if "const" in schema:
        return schema["const"]
    if schema.get("enum"):
        return schema["enum"][0]
    if schema.get("default") is not None:
        return copy.deepcopy(schema["default"])
    kind = schema.get("type")
    if kind == "object" or "properties" in schema:
        props = schema.get("properties", {})
        return {k: sample(props.get(k, {}), depth + 1) for k in schema.get("required", [])}
    if kind == "array":
        n = schema.get("minItems", 0)
        return [sample(schema.get("items", {}), depth + 1) for _ in range(n)]
    if kind == "string":
        fmt = schema.get("format")
        if fmt == "date-time":
            return (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
        if fmt == "date":
            return (date.today() + timedelta(days=3)).isoformat()
        if fmt == "uuid":
            return str(uuid.uuid4())
        if fmt == "email":
            return "someone@example.com"
        if fmt in ("uri", "url"):
            return "https://example.com"
        n = max(schema.get("minLength", 1), 1)
        return ("x" * n)[: schema.get("maxLength", n)]
    if kind in ("integer", "number"):
        low = schema.get("minimum", schema.get("exclusiveMinimum", 0) + 1 if "exclusiveMinimum" in schema else 1)
        value = max(1, low)
        if "maximum" in schema:
            value = min(value, schema["maximum"])
        return int(value) if kind == "integer" else float(value)
    if kind == "boolean":
        return False
    return None


def request_for(route: str, event_id: int) -> dict:
    method, path = route.split(" ", 1)
    op = _spec()["paths"][path][method.lower()]
    url, query = path, {}
    for p in op.get("parameters", []):
        if p["in"] == "path":
            value = event_id if p["name"] == "event_id" else sample(p.get("schema", {}))
            if p["name"] != "event_id" and isinstance(value, int):
                value = 987654
            url = url.replace("{" + p["name"] + "}", str(value))
        elif p["in"] == "query" and p.get("required"):
            query[p["name"]] = sample(p.get("schema", {}))
    kwargs = dict(method=method, url=url, params=query)
    body = op.get("requestBody", {}).get("content", {})
    if "application/json" in body:
        kwargs["json"] = sample(body["application/json"].get("schema", {}))
    return kwargs


def _answer(r) -> tuple:
    try:
        data = r.json()
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    message = re.sub(r"\d+", "#", str(data.get("message") or "")) if r.status_code >= 400 else ""
    return r.status_code, data.get("error_code"), message


# ─── The world: a host, a co-host, a stranger, two twin events ──────────────


@pytest.fixture
async def world(session, make_club, make_event, mint_token, monkeypatch):
    monkeypatch.setattr(limiter, "enabled", False)
    host_owner, host = await make_club(name="Design Society", email="design@example.com")
    cohost_owner, cohost = await make_club(name="Robotics Club", email="robotics@example.com")
    stranger_owner, _stranger = await make_club(name="Chess Circle", email="chess@example.com")
    starts = datetime.now(timezone.utc) + timedelta(days=3)
    events = []
    for _ in range(2):
        e = await make_event(club=host, name="Hack the Campus", created_by=host_owner, event_datetime=starts, lifecycle=EventStatus.published)
        session.add(EventCohosts(
            event_id=e.id, club_id=cohost.id, status=ACCEPTED, invited_at=datetime.now(timezone.utc),
            expires_at=starts, responded_at=datetime.now(timezone.utc),
        ))
        events.append(e)
    await session.commit()
    return dict(
        events=events,
        host=auth_header(await mint_token(host_owner)),
        cohost=auth_header(await mint_token(cohost_owner)),
        stranger=auth_header(await mint_token(stranger_owner)),
    )


class _Raised:
    """A route that raised past its handlers (the suite has no S3 keys, no
    wkhtmltoimage): compared by the error's type, as an answer would be."""

    def __init__(self, exc: BaseException):
        self.status_code = 599
        self.text = f"raised {type(exc).__name__}: {exc}"[:300]
        self._name = type(exc).__name__

    def json(self):
        return {"message": self._name, "error_code": "RAISED"}


async def _call(client, headers, route, event_id):
    try:
        return await client.request(headers=headers, **request_for(route, event_id))
    except Exception as exc:  # noqa: BLE001
        return _Raised(exc)


REFUSED = {401, 403, 404}
#: Routes that answered a stranger club 200 before co-hosts, with nothing of
#: the event in it: kept as they were (the dashboard reads them that way).
STRANGER_SOFT = {
    # {"success": false, "message": "You don't have permission ..."}
    "POST /api/v1/clubs/notifications/events/{event_id}/send",
    # zeros
    "GET /api/v1/clubs/notifications/events/{event_id}/stats",
}


def _refused(strange, as_host, route) -> bool:
    if strange.status_code in REFUSED:
        return True
    if route in STRANGER_SOFT and strange.status_code == 200:
        return True
    # The request itself was malformed (the same for the host): refused before any route ran.
    return strange.status_code == 422 and as_host.status_code == 422


@pytest.mark.parametrize("route", sorted(r for r, kind in ROUTES.items() if kind != COHOST_ONLY))
async def test_a_route_treats_a_cohost_as_the_host(client, world, route):
    kind = ROUTES[route]
    one, twin = world["events"]
    strange = await _call(client, world["stranger"], route, one.id)
    as_cohost = await _call(client, world["cohost"], route, one.id)
    as_host = await _call(client, world["host"], route, twin.id)
    if kind == HOST_ONLY:
        assert _answer(as_cohost)[:2] == (403, "COHOST_HOST_ONLY"), as_cohost.text
        assert as_host.status_code != 403 or _answer(as_host)[1] != "COHOST_HOST_ONLY", as_host.text
        assert _refused(strange, as_host, route), strange.text
        return
    assert _answer(as_cohost) == _answer(as_host), f"co-host: {as_cohost.status_code} {as_cohost.text[:300]} / host: {as_host.status_code} {as_host.text[:300]}"
    if kind == ALLOW:
        assert _refused(strange, as_host, route), f"a stranger club got {strange.status_code}: {strange.text[:300]}"


# ─── The table itself ───────────────────────────────────────────────────────
#
# Last in the file: a test that opens no database first in a run sets the
# suite's loop up apart from the session's engine (tests/conftest.py), and
# every request after it fails.


def test_every_club_side_event_route_is_in_the_table():
    found = club_side_event_routes()
    missing = sorted(found - set(ROUTES))
    gone = sorted(set(ROUTES) - found)
    assert not missing, "Classify these routes for co-hosts in ROUTES (and cohosts.policy if host-only): " + ", ".join(missing)
    assert not gone, "These routes are gone; take them out of ROUTES: " + ", ".join(gone)


def _concrete(path: str) -> str:
    return re.sub(r"\{event_id\}", "7", re.sub(r"\{(?!event_id)[^}]+\}", "9", path))


@pytest.mark.parametrize("route", sorted(ROUTES))
def test_the_policy_says_what_the_table_says(route):
    method, path = route.split(" ", 1)
    assert policy.host_only(method, _concrete(path)) == (ROUTES[route] == HOST_ONLY)
