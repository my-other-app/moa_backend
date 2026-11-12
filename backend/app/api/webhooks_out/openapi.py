"""The webhooks in the partner API's OpenAPI document (app/api/partner/openapi.py
calls `add_to`): OpenAPI 3.1's top-level `webhooks`, one POST per type, each
with its body's schema -- the envelope, and `data.object` as the GET's own
schema (EventOut, RegistrationOut, CheckinOut, CertificateOut, ClubOut) --
and the headers it is sent with.
"""
from __future__ import annotations

from app.api.webhooks_out import types as wtypes

#: The GET's schema for each object, by name in components.schemas.
OBJECT_SCHEMAS = {
    "event": "EventOut", "registration": "RegistrationOut", "checkin": "CheckinOut",
    "certificate": "CertificateOut", "club": "ClubOut", "payment": "WebhookPayment", "test": "WebhookTestObject",
}

SIGNING = (
    "Sent as a POST with `MOA-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256(secret, \"<t>.<raw body>\")>` -- the "
    "key is the endpoint's whole secret (`whsec_...`); during a secret rotation a second `v1=` is the old secret's. "
    "Check it over the raw body and refuse a `t` more than 5 minutes off. Answer 2xx within 10 seconds; anything else "
    "is tried again after 1 m, 5 m, 30 m, 2 h, 6 h, 12 h and 24 h. At least once: drop repeats by `MOA-Event-Id`. "
    "People's names, emails, phones and answers are in `data.object` (and `previous`) only for an endpoint whose owner "
    "may have them (attendees:read, or the club's DPDP tick)."
)

HEADERS = [
    ("MOA-Signature", True, "t=<unix seconds>,v1=<hex HMAC-SHA256>[,v1=<the old secret's, during a rotation>]"),
    ("MOA-Event-Id", True, "The body's `id` (evt_...): drop a repeat by it."),
    ("MOA-Event-Type", True, "The body's `type`."),
    ("MOA-Delivery-Id", True, "This delivery (the endpoint's log)."),
    ("MOA-Delivery-Attempt", True, "1 for the first try."),
]


def _schemas() -> dict:
    return {
        "WebhookOrigin": {
            "type": "object", "required": ["type"],
            "properties": {
                "type": {"type": "string", "enum": ["dashboard", "app", "website", "api_key", "partner_app", "system"]},
                "client_id": {"type": "string", "description": "The API key's or partner app's client id, for a change made through the API."},
            },
        },
        "WebhookPayment": {
            "type": "object", "description": "A ticket's online payment: amounts and status only.",
            "required": ["id", "object", "status", "amount", "amount_refunded", "currency", "livemode"],
            "properties": {
                "id": {"type": "string"}, "object": {"type": "string", "const": "payment"},
                "registration_id": {"type": ["string", "null"]}, "event_id": {"type": ["string", "null"]},
                "status": {"type": "string", "enum": ["captured", "refunded", "partially_refunded"]},
                "amount": {"type": "integer", "description": "Paise."}, "amount_refunded": {"type": "integer", "description": "Paise."},
                "currency": {"type": "string", "const": "INR"}, "livemode": {"type": "boolean"},
                "created_at": {"type": ["string", "null"], "format": "date-time"}, "updated_at": {"type": ["string", "null"], "format": "date-time"},
            },
        },
        "WebhookTestObject": {
            "type": "object", "required": ["object", "id", "message", "livemode"],
            "properties": {"object": {"type": "string", "const": "test"}, "id": {"type": "string", "description": "The endpoint's id."},
                           "message": {"type": "string"}, "livemode": {"type": "boolean"}},
        },
    }


def _object(name: str, schemas: dict) -> dict:
    ref = OBJECT_SCHEMAS.get(name)
    schema = {"$ref": f"#/components/schemas/{ref}"} if ref in schemas else {"type": "object"}
    if name == "checkin":
        schema = {"allOf": [schema, {"type": "object", "properties": {"deleted": {"type": "boolean", "description": "True for checkin.deleted: the check-in as it was."}}}]}
    return schema


def schema_name(event_type: str) -> str:
    return "Webhook_" + event_type.replace(".", "_")


def add_to(doc: dict) -> dict:
    schemas = doc.setdefault("components", {}).setdefault("schemas", {})
    schemas.update(_schemas())
    hooks = doc.setdefault("webhooks", {})
    every = [(t, obj, text) for t, (obj, text) in wtypes.EVENT_TYPES.items()]
    every.append((wtypes.TEST_TYPE, "test", "Sent by a test (POST /webhook-endpoints/{id}/test) to that endpoint only."))
    for event_type, obj, text in every:
        name = schema_name(event_type)
        schemas[name] = {
            "title": event_type, "type": "object",
            "required": ["id", "type", "created_at", "livemode", "club_id", "origin", "data"],
            "properties": {
                "id": {"type": "string", "description": "evt_...: the same as MOA-Event-Id."},
                "type": {"type": "string", "const": event_type},
                "created_at": {"type": "string", "format": "date-time"},
                "livemode": {"type": "boolean", "description": "False for a test-mode object (moa_test_ keys): only test endpoints get it."},
                "club_id": {"type": "string"},
                "origin": {"$ref": "#/components/schemas/WebhookOrigin"},
                "data": {
                    "type": "object", "required": ["object"],
                    "properties": {
                        "object": _object(obj, schemas),
                        "previous": {"type": "object", "additionalProperties": True,
                                     "description": "For updates: the top-level fields that changed, as they were."},
                    },
                },
            },
        }
        hooks[event_type] = {
            "post": {
                "summary": text, "description": SIGNING, "operationId": name, "tags": ["webhooks"], "security": [],
                "parameters": [
                    {"name": header, "in": "header", "required": required, "schema": {"type": "string"}, "description": desc}
                    for header, required, desc in HEADERS
                ],
                "requestBody": {"required": True, "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{name}"}}}},
                "responses": {"2XX": {"description": "Received. Answer within 10 seconds; do the work after."}},
            }
        }
    return doc
