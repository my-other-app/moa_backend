"""Webhooks and the partner API together, through /partner/v1 with real keys,
the real allocation and the real seams (app/api/webhooks_out/wiring.py):
no echo to the key's own endpoint, the change told to the club's other one
with origin api_key and a signature that verifies, test mode never reaching
a live endpoint, and the routes and payloads in the OpenAPI document."""
from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app.api.partner import allocation
from app.api.webhooks_out import crypto, seams
from app.api.webhooks_out.models import WebhookEndpoints
from app.api.webhooks_out.types import EVENT_TYPES
from tests.conftest import auth_header
from tests.test_partner_api import EVERYTHING, H, event_body, key_for, partner_on, person, world  # noqa: F401
from tests.webhooks_helpers import real_hub  # noqa: F401

V1 = "/partner/v1"
DASH = "/api/v1/clubs/integrations/webhooks"
SCOPES = [*EVERYTHING, "webhooks:manage"]


async def test_seams_answer_from_the_partner_api(session, world, real_hub):
    assert await seams.club_allocated(session, world.ids.club) is True
    assert await seams.club_allocated(session, world.ids.other_club) is False
    await allocation.put(session, world.ids.club, enabled=False)
    assert await seams.club_allocated(session, world.ids.club) is False
    assert await seams.partner_scopes(session, world.ids.club, None, "moa_app_nobody") == frozenset()


async def _dashboard_endpoint(client, mint_token, owner, url, **extra) -> tuple[str, str]:
    r = await client.post(DASH, json={"url": url, "events": ["*"], **extra}, headers=auth_header(await mint_token(owner)))
    assert r.status_code == 201, r.text
    return r.json()["id"], r.json()["secret"]


async def test_a_key_s_own_changes_reach_the_club_s_other_endpoint_only(client, session, world, mint_token, real_hub):
    hub = real_hub
    raw = await key_for(session, world.ids.club, SCOPES)
    me = (await client.get(f"{V1}/me", headers=H(raw))).json()
    key_client = me["client_id"]

    # The key's own endpoint (it belongs to the key: no echo), through the partner API.
    r = await client.post(f"{V1}/webhook-endpoints", json={"url": "https://erp.example.com/moa", "events": ["*"]},
                          headers=H(raw, **{"Idempotency-Key": "ep-1"}))
    assert r.status_code == 201, r.text
    own = r.json()
    assert own["secret"].startswith("whsec_") and own["owner"]["client_id"] == key_client and own["livemode"] is True
    assert r.headers.get("X-Request-Id")
    # The club's website endpoint, added in the dashboard with the DPDP tick.
    site_id, site_secret = await _dashboard_endpoint(client, mint_token, world.owner, "https://site.example.com/moa",
                                                     share_attendee_details=True, dpdp_confirmed=True)
    # A test-mode endpoint of the club's: it must never get a live change.
    await _dashboard_endpoint(client, mint_token, world.owner, "https://sandbox.example.com/moa", livemode=False)
    hub.clear()

    reg = await client.post(f"{V1}/events/{world.ids.upcoming}/registrations", json=person(1), headers=H(raw))
    assert reg.status_code == 201, reg.text
    reg = reg.json()
    checkin = await client.post(f"{V1}/checkins", json={"registration_id": reg["id"]}, headers=H(raw))
    assert checkin.status_code in (200, 201), checkin.text

    hosts = [(r.headers["Host"], json.loads(r.content)["type"]) for r in hub.requests]
    assert hosts == [("site.example.com", "registration.created"), ("site.example.com", "checkin.created")], hosts
    for request in hub.requests:
        body = json.loads(request.content)
        assert body["origin"] == {"type": "api_key", "client_id": key_client}
        assert body["livemode"] is True and body["club_id"] == str(world.ids.club)
        assert crypto.verify(site_secret, request.headers["MOA-Signature"], request.content)
        assert request.headers["MOA-Event-Id"] == body["id"]
    created = json.loads(hub.requests[0].content)["data"]["object"]
    got = (await client.get(f"{V1}/registrations/{reg['id']}", headers=H(raw))).json()
    assert created["id"] == got["id"] and created["status"] == got["status"] and created["person"] == got["person"]
    assert created["payment"] == got["payment"] and created["approval"] == got["approval"]
    assert json.loads(hub.requests[1].content)["data"]["object"]["registration_id"] == reg["id"]

    # The key's own endpoint opts in: now it hears its own changes too.
    r = await client.patch(f"{V1}/webhook-endpoints/{own['id']}", json={"include_own_changes": True}, headers=H(raw))
    assert r.status_code == 200 and r.json()["include_own_changes"] is True
    hub.clear()
    r = await client.post(f"{V1}/events/{world.ids.upcoming}/registrations", json=person(2), headers=H(raw))
    assert r.status_code == 201, r.text
    assert sorted(r.headers["Host"] for r in hub.requests) == ["erp.example.com", "site.example.com"]


async def test_a_test_mode_key_s_changes_never_reach_a_live_endpoint(client, session, world, mint_token, real_hub):
    hub = real_hub
    await _dashboard_endpoint(client, mint_token, world.owner, "https://live.example.com/moa")
    _test_id, test_secret = await _dashboard_endpoint(client, mint_token, world.owner, "https://test.example.com/moa", livemode=False)
    test_key = await key_for(session, world.ids.club, SCOPES, livemode=False)
    test_client = (await client.get(f"{V1}/me", headers=H(test_key))).json()["client_id"]
    hub.clear()

    event_id = (await client.post(f"{V1}/events", json=event_body(), headers=H(test_key))).json()["id"]
    assert (await client.post(f"{V1}/events/{event_id}/publish", headers=H(test_key))).status_code == 200
    reg = (await client.post(f"{V1}/events/{event_id}/registrations", json=person(1), headers=H(test_key))).json()
    await client.post(f"{V1}/checkins", json={"registration_id": reg["id"]}, headers=H(test_key))

    assert {r.headers["Host"] for r in hub.requests} == {"test.example.com"}
    types = [json.loads(r.content)["type"] for r in hub.requests]
    assert types == ["event.created", "event.published", "registration.created", "checkin.created"]
    for request in hub.requests:
        body = json.loads(request.content)
        assert body["livemode"] is False and body["data"]["object"]["livemode"] is False
        assert body["origin"] == {"type": "api_key", "client_id": test_client}
        assert crypto.verify(test_secret, request.headers["MOA-Signature"], request.content)
    # A test key's own endpoint is in test mode: it cannot ask for a live one.
    r = await client.post(f"{V1}/webhook-endpoints", json={"url": "https://x.example.com/moa", "livemode": True}, headers=H(test_key))
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION"


async def test_the_partner_api_s_error_shape_and_scope(client, session, world, real_hub):
    no_scope = await key_for(session, world.ids.club, EVERYTHING)
    r = await client.get(f"{V1}/webhook-endpoints", headers=H(no_scope))
    assert r.status_code == 403 and r.json()["error"]["code"] == "FORBIDDEN_SCOPE"
    raw = await key_for(session, world.ids.club, SCOPES)
    r = await client.post(f"{V1}/webhook-endpoints", json={"url": "http://plain.example.com/x"}, headers=H(raw))
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION" and r.json()["error"]["request_id"]
    r = await client.get(f"{V1}/webhook-endpoints/not-an-id", headers=H(raw))
    assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND"
    # Idempotent: the same key twice is one endpoint, the first answer again.
    first = await client.post(f"{V1}/webhook-endpoints", json={"url": "https://once.example.com/x"}, headers=H(raw, **{"Idempotency-Key": "k-1"}))
    again = await client.post(f"{V1}/webhook-endpoints", json={"url": "https://once.example.com/x"}, headers=H(raw, **{"Idempotency-Key": "k-1"}))
    assert first.status_code == 201 and again.json()["id"] == first.json()["id"]
    assert len(list(await session.scalars(select(WebhookEndpoints).where(WebhookEndpoints.url == "https://once.example.com/x")))) == 1


async def test_the_openapi_document_has_the_routes_and_every_payload(client, session, world):
    from app.api.partner import openapi

    openapi.reset()
    # The document is private (tests/test_dev_docs_links.py): any key opens it.
    doc = (await client.get(f"{V1}/openapi.json", headers=H(await key_for(session, world.ids.club, ["events:read"])))).json()
    assert doc["paths"]["/partner/v1/webhook-endpoints"]["post"]["x-scopes"] == ["webhooks:manage"]
    assert "/partner/v1/webhook-deliveries/{delivery_id}/retry" in doc["paths"]
    assert set(doc["webhooks"]) == set(EVENT_TYPES) | {"webhook.test"}
    body = doc["components"]["schemas"]["Webhook_registration_created"]
    assert body["properties"]["data"]["properties"]["object"] == {"$ref": "#/components/schemas/RegistrationOut"}
    assert "RegistrationOut" in doc["components"]["schemas"] and "WebhookPayment" in doc["components"]["schemas"]
    hook = doc["webhooks"]["payment.refunded"]["post"]
    assert [p["name"] for p in hook["parameters"]][:2] == ["MOA-Signature", "MOA-Event-Id"]


@pytest.fixture(autouse=True)
def _openapi_fresh():
    from app.api.partner import openapi

    openapi.reset()
    yield
    openapi.reset()


# Last in the file: a test without the event loop that runs before the async
# ones leaves them on a loop other than the test engine's (tests/test_cov_sweeps.py).
def test_the_seams_are_installed_at_start_up():
    """Without these nothing is ever sent: no club would count as allocated."""
    import doth  # noqa: F401 - what the API and the worker import
    from app.api.webhooks_out import wiring

    assert sorted(seams.installed()) == ["club_allocated", "external_id", "partner_contact", "partner_scopes"]
    assert seams._base["club_allocated"] is wiring.club_allocated
