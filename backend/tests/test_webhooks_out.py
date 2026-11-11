"""Outgoing webhooks: endpoints, emission, signed delivery, retries, turning
off, the SSRF guard, the delivery log, secrets (app/api/webhooks_out,
docs/WEBHOOKS.md). Every HTTP call is caught (tests/webhooks_helpers.py)."""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import func, select

from app.api.club_alerts.models import ClubAlerts
from app.api.webhooks_out import crypto, delivery, objects, service, sweep
from app.api.webhooks_out.emit import Ref, emit
from app.api.webhooks_out.models import FAILED, RETRYING, SUCCEEDED, WebhookDeliveries, WebhookEndpoints, WebhookEvents
from app.config import settings
from app.core import jobs, net
from app.core.api_origin import Origin, reset_origin, set_origin
from tests.conftest import auth_header
from tests.webhooks_helpers import PUBLIC_IP, add_endpoint, deliver_all, events_of, hub  # noqa: F401

BASE = "/api/v1/clubs/integrations/webhooks"


@pytest.fixture
async def club(make_club, hub):
    user, club = await make_club(name="Hook Club", email="hooks@club.example")
    hub.allocated.add(club.id)
    return user, club


@pytest.fixture
async def headers(club, mint_token):
    return auth_header(await mint_token(club[0]))


async def _event(make_event, club, **kw):
    user, c = club
    return await make_event(club=c, created_by=user, name=kw.pop("name", "Hack Night"), **kw)


# ─── Endpoints: made, shown, refused ─────────────────────────────────────


async def test_secret_is_shown_once_and_kept_encrypted(client, session, headers, club):
    r = await client.post(BASE, json={"url": "https://hooks.example.com/moa", "events": ["event.created", "event.updated"]}, headers=headers)
    assert r.status_code == 201, r.text
    body = r.json()
    secret = body["secret"]
    assert secret.startswith("whsec_") and len(secret) > 40
    assert body["secret_hint"].endswith(secret[-4:])
    row = await session.get(WebhookEndpoints, body["id"])
    assert row.secret_enc and secret not in row.secret_enc
    assert crypto.open_(row.secret_enc) == secret
    again = await client.get(f"{BASE}/{body['id']}", headers=headers)
    assert again.status_code == 200 and "secret" not in again.json()
    listed = await client.get(BASE, headers=headers)
    assert [e["id"] for e in listed.json()["data"]] == [body["id"]]
    assert "secret" not in listed.text.replace("secret_hint", "").replace("secret_rotated_at", "").replace("previous_secret_expires_at", "")


@pytest.mark.parametrize("url,word", [
    ("http://hooks.example.com/moa", "https"),
    ("https://10.1.2.3/moa", "private"),
    ("https://127.0.0.1/moa", "private"),
    ("https://[::1]/moa", "private"),
    ("https://[::ffff:10.0.0.1]/moa", "private"),
    ("https://169.254.169.254/latest/meta-data", "private"),
    ("https://localhost/moa", "private"),
    ("https://printer.local/moa", "private"),
    ("https://internal.example.com/moa", "private"),
    ("https://user:pw@hooks.example.com/moa", "password"),
    ("ftp://hooks.example.com/", "https"),
])
async def test_unsafe_addresses_are_refused(client, headers, url, word):
    r = await client.post(BASE, json={"url": url}, headers=headers)
    assert r.status_code == 422, r.text
    assert word in r.json()["message"].lower()


async def test_unknown_event_types_are_refused(client, headers):
    r = await client.post(BASE, json={"url": "https://hooks.example.com/x", "events": ["event.exploded"]}, headers=headers)
    assert r.status_code == 422 and "event.exploded" in r.json()["message"]


async def test_unavailable_without_the_key_and_unallocated_clubs(client, headers, club, hub, monkeypatch):
    hub.allocated.discard(club[1].id)
    r = await client.post(BASE, json={"url": "https://hooks.example.com/x"}, headers=headers)
    assert r.status_code == 403 and r.json()["error_code"] == "NOT_ALLOCATED"
    hub.allocated.add(club[1].id)
    monkeypatch.setattr(settings, "WEBHOOK_KEYS_SECRET", "")
    r = await client.post(BASE, json={"url": "https://hooks.example.com/x"}, headers=headers)
    assert r.status_code == 503 and "APP_WEBHOOK_KEYS_SECRET" in r.json()["message"]
    status = (await client.get(f"{BASE}/status", headers=headers)).json()
    assert status["available"] is False and status["reason"] == "unavailable"


async def test_editors_and_door_staff_are_refused(client, headers, monkeypatch):
    from app.api.club_access import service as club_access

    monkeypatch.setattr(club_access, "role_of", lambda request: "editor")
    r = await client.get(BASE, headers=headers)
    assert r.status_code == 403 and r.json()["error_code"] == "ROLE_FORBIDDEN"


async def test_contact_details_need_the_dpdp_statement(client, headers):
    r = await client.post(BASE, json={"url": "https://hooks.example.com/x", "share_attendee_details": True}, headers=headers)
    assert r.status_code == 422 and "hooks.example.com" in r.json()["message"]
    r = await client.post(BASE, json={"url": "https://hooks.example.com/x", "share_attendee_details": True, "dpdp_confirmed": True}, headers=headers)
    assert r.status_code == 201 and r.json()["share_attendee_details"] is True and r.json()["dpdp_confirmed_at"]


# ─── Emission and delivery ───────────────────────────────────────────────


async def test_a_change_is_sent_signed_after_its_commit(session, club, make_event, hub):
    _, c = club
    endpoint, secret = await add_endpoint(session, c.id)
    event = await _event(make_event, club)
    await emit(session, c.id, "event.created", Ref("event", event.id))
    await jobs.settle()
    assert hub.requests == []  # nothing before the commit
    await deliver_all(session)
    assert len(hub.requests) == 1
    request = hub.requests[0]
    raw = request.content
    body = json.loads(raw)
    assert body["type"] == "event.created" and body["id"].startswith("evt_")
    assert body["club_id"] == str(c.id) and body["livemode"] is True
    assert body["origin"] == {"type": "system"}
    assert body["data"]["object"]["object"] == "event" and body["data"]["object"]["id"] == str(event.id)
    assert body["data"]["object"]["name"] == "Hack Night"
    assert request.headers["MOA-Event-Id"] == body["id"]
    assert request.headers["Content-Type"] == "application/json"
    # The documented recipe: HMAC-SHA256 over "<t>.<raw body>" with the whole secret.
    header = request.headers["MOA-Signature"]
    parts = dict(p.split("=", 1) for p in header.split(","))
    expected = hmac.new(secret.encode(), f"{parts['t']}.".encode() + raw, hashlib.sha256).hexdigest()
    assert parts["v1"] == expected
    assert crypto.verify(secret, header, raw)
    assert not crypto.verify(secret, header, raw + b" ")
    assert not crypto.verify("whsec_wrong", header, raw)
    assert not crypto.verify(secret, header, raw, now=int(parts["t"]) + 301)
    # Connected to the address that was checked, with the host name for TLS.
    assert request.url.host == PUBLIC_IP
    assert request.headers["Host"] == "hooks.example.com"
    assert request.extensions.get("sni_hostname") == "hooks.example.com"
    row = await session.scalar(select(WebhookDeliveries).where(WebhookDeliveries.endpoint_id == endpoint.id))
    assert row.status == SUCCEEDED and row.attempts == 1 and row.response_status == 200 and row.duration_ms is not None


async def test_a_rolled_back_change_sends_nothing(session, club, make_event, hub):
    _, c = club
    await add_endpoint(session, c.id)
    event = await _event(make_event, club)
    await session.commit()
    await emit(session, c.id, "event.updated", Ref("event", event.id))
    await session.rollback()
    await deliver_all(session)
    assert hub.requests == []
    assert await events_of(session) == []


async def test_a_savepoint_rolled_back_takes_its_change_with_it(session, club, make_event, hub):
    _, c = club
    await add_endpoint(session, c.id)
    event = await _event(make_event, club)
    await session.commit()
    try:
        async with session.begin_nested():
            await emit(session, c.id, "event.updated", Ref("event", event.id))
            raise RuntimeError("the change failed")
    except RuntimeError:
        pass
    await emit(session, c.id, "event.published", Ref("event", event.id))
    await deliver_all(session)
    assert hub.types() == ["event.published"]


async def test_nothing_is_written_for_a_club_without_endpoints(session, club, make_event, hub):
    _, c = club
    event = await _event(make_event, club)
    assert await emit(session, c.id, "event.created", Ref("event", event.id)) is None
    await deliver_all(session)
    assert await events_of(session) == []


async def test_an_unallocated_club_queues_nothing(session, club, make_event, hub):
    _, c = club
    await add_endpoint(session, c.id)
    hub.allocated.discard(c.id)
    event = await _event(make_event, club)
    assert await emit(session, c.id, "event.created", Ref("event", event.id)) is None


async def test_endpoints_get_only_the_types_they_chose(session, club, make_event, hub):
    _, c = club
    await add_endpoint(session, c.id, url="https://a.example.com/x", events=["event.published"])
    await add_endpoint(session, c.id, url="https://b.example.com/x", events=["*"])
    event = await _event(make_event, club)
    await emit(session, c.id, "event.created", Ref("event", event.id))
    await emit(session, c.id, "event.published", Ref("event", event.id))
    await deliver_all(session)
    by_host = sorted((r.headers["Host"], json.loads(r.content)["type"]) for r in hub.requests)
    assert by_host == [("a.example.com", "event.published"), ("b.example.com", "event.created"), ("b.example.com", "event.published")]


async def test_no_echo_to_the_client_that_made_the_change(session, club, make_event, hub):
    _, c = club
    owner = service.Owner(club_id=c.id, via="api_key", client_id="key_A", livemode=True, scopes=frozenset({"webhooks:manage"}))
    await add_endpoint(session, c.id, url="https://own.example.com/x", owner=owner)
    opted = service.Owner(club_id=c.id, via="api_key", client_id="key_A", livemode=True, scopes=frozenset({"webhooks:manage"}))
    await add_endpoint(session, c.id, url="https://optin.example.com/x", owner=opted, include_own_changes=True)
    await add_endpoint(session, c.id, url="https://other.example.com/x")
    event = await _event(make_event, club)
    token = set_origin(Origin(type="api_key", client_id="key_A"))
    try:
        await emit(session, c.id, "event.updated", Ref("event", event.id))
    finally:
        reset_origin(token)
    await deliver_all(session)
    hosts = sorted(r.headers["Host"] for r in hub.requests)
    assert hosts == ["optin.example.com", "other.example.com"]
    assert all(json.loads(r.content)["origin"] == {"type": "api_key", "client_id": "key_A"} for r in hub.requests)
    hub.clear()
    # Another client's change reaches the key's endpoint.
    await emit(session, c.id, "event.updated", Ref("event", event.id), Origin(type="api_key", client_id="key_B"))
    await deliver_all(session)
    assert "own.example.com" in {r.headers["Host"] for r in hub.requests}


async def test_live_and_test_mode_never_mix(session, club, make_event, hub):
    _, c = club
    await add_endpoint(session, c.id, url="https://live.example.com/x")
    await add_endpoint(session, c.id, url="https://test.example.com/x", livemode=False)
    event = await _event(make_event, club)
    await emit(session, c.id, "event.updated", Ref("event", event.id))
    await emit(session, c.id, "event.updated", {"object": "event", "id": "999", "livemode": False})
    await deliver_all(session)
    got = sorted((r.headers["Host"], json.loads(r.content)["livemode"]) for r in hub.requests)
    assert got == [("live.example.com", True), ("test.example.com", False)]


async def test_contact_fields_only_for_owners_allowed_them(session, club, make_event, make_registration, make_user, hub):
    _, c = club
    await add_endpoint(session, c.id, url="https://plain.example.com/x")
    await add_endpoint(session, c.id, url="https://dpdp.example.com/x", share_attendee_details=True, dpdp_confirmed=True)
    reader = service.Owner(club_id=c.id, via="partner_app", client_id="partner_acme", scopes=frozenset({"webhooks:manage"}))
    await add_endpoint(session, c.id, url="https://acme.example.com/x", owner=reader)
    blind = service.Owner(club_id=c.id, via="partner_app", client_id="partner_blind", scopes=frozenset({"webhooks:manage"}))
    await add_endpoint(session, c.id, url="https://blind.example.com/x", owner=blind)
    nothing = service.Owner(club_id=c.id, via="partner_app", client_id="partner_events_only", scopes=frozenset({"webhooks:manage"}))
    await add_endpoint(session, c.id, url="https://eventsonly.example.com/x", owner=nothing)
    hub.partner_scopes[(c.id, "partner_acme")] = frozenset({"registrations:read", "attendees:read"})
    hub.partner_scopes[(c.id, "partner_blind")] = frozenset({"registrations:read"})
    hub.partner_scopes[(c.id, "partner_events_only")] = frozenset({"events:read"})
    event = await _event(make_event, club)
    person = await make_user(email="asha@example.com")
    reg = await make_registration(event=event, user=person, phone="9876543210", additional_details={"college": "CET"})
    await emit(session, c.id, "registration.created", Ref("registration", reg.id))
    await deliver_all(session)
    objs = {r.headers["Host"]: json.loads(r.content)["data"]["object"] for r in hub.requests}
    assert set(objs) == {"plain.example.com", "dpdp.example.com", "acme.example.com", "blind.example.com"}
    for host in ("plain.example.com", "blind.example.com"):
        assert "person" not in objs[host] and "answers" not in objs[host], host
        assert objs[host]["ticket"]["code"] == reg.ticket_id
    for host in ("dpdp.example.com", "acme.example.com"):
        assert objs[host]["person"] == {"name": person.full_name, "email": "asha@example.com", "phone": "9876543210"}
        assert objs[host]["answers"] == {"college": "CET"}
    # External ids are per client: the partner's own.
    assert objs["acme.example.com"]["external_id"] == f"ext-registration-{reg.id}"
    assert objs["plain.example.com"]["external_id"] is None


async def test_a_partner_disconnected_gets_nothing(session, club, make_event, hub):
    _, c = club
    owner = service.Owner(club_id=c.id, via="partner_app", client_id="partner_gone", scopes=frozenset({"webhooks:manage"}))
    await add_endpoint(session, c.id, url="https://gone.example.com/x", owner=owner)
    event = await _event(make_event, club)
    await emit(session, c.id, "event.updated", Ref("event", event.id))
    await deliver_all(session)
    assert hub.requests == []


# ─── Retries, turning off, telling the owner ─────────────────────────────


async def test_failures_are_retried_on_the_schedule_then_fail(session, club, make_event, hub):
    _, c = club
    endpoint, _ = await add_endpoint(session, c.id)
    hub.reply(500, "boom")
    event = await _event(make_event, club)
    await emit(session, c.id, "event.updated", Ref("event", event.id))
    await deliver_all(session)
    row = await session.scalar(select(WebhookDeliveries))
    assert row.status == RETRYING and row.attempts == 1 and row.response_status == 500 and row.response_body == "boom"
    wait = (row.next_attempt_at - row.last_attempt_at).total_seconds()
    assert 59 <= wait <= 61
    queued = await session.scalar(
        select(func.count()).select_from(jobs.Jobs).where(jobs.Jobs.name == delivery.DELIVER_JOB, jobs.Jobs.finished_at.is_(None))
    )
    assert queued == 1
    waits = []
    for n in range(1, delivery.MAX_ATTEMPTS):
        before = row.attempts
        await delivery.attempt(session, row.id, n)
        row = await session.get(WebhookDeliveries, row.id, populate_existing=True)
        assert row.attempts == before + 1
        if row.status == RETRYING:
            waits.append(round((row.next_attempt_at - row.last_attempt_at).total_seconds()))
    assert waits == [300, 1800, 7200, 21600, 43200, 86400]
    assert row.status == FAILED and row.attempts == delivery.MAX_ATTEMPTS
    ep = await session.get(WebhookEndpoints, endpoint.id, populate_existing=True)
    assert ep.enabled and ep.consecutive_failures == delivery.MAX_ATTEMPTS and ep.failing_since is not None
    # A stale job for a try already made changes nothing.
    assert await delivery.attempt(session, row.id, 2) is None


async def test_three_days_of_failure_turn_the_endpoint_off_and_tell_the_owner(session, club, make_event, hub, monkeypatch):
    from app.api.subscriptions import notices

    sent = []
    monkeypatch.setattr(notices, "send", lambda to, subject, paragraphs, button, footer: sent.append((to, subject)))
    _, c = club
    endpoint, _ = await add_endpoint(session, c.id)
    hub.reply(503)
    event = await _event(make_event, club)
    await emit(session, c.id, "event.updated", Ref("event", event.id))
    await emit(session, c.id, "event.published", Ref("event", event.id))
    await session.commit()
    ep = await session.get(WebhookEndpoints, endpoint.id)
    ep.failing_since = datetime.now(timezone.utc) - timedelta(days=3, minutes=1)
    await deliver_all(session)
    ep = await session.get(WebhookEndpoints, endpoint.id, populate_existing=True)
    assert ep.enabled is False and ep.disabled_reason == "failing"
    rows = list(await session.scalars(select(WebhookDeliveries)))
    assert {r.status for r in rows} == {FAILED}
    alert = await session.scalar(select(ClubAlerts).where(ClubAlerts.club_id == c.id, ClubAlerts.kind == "webhook_disabled"))
    assert alert is not None and "hooks.example.com" in alert.title
    await deliver_all(session)
    assert sent == [("hooks@club.example", alert.title)]
    # Turned off: nothing more is queued for it.
    hub.clear()
    await emit(session, c.id, "event.updated", Ref("event", event.id))
    await deliver_all(session)
    assert hub.requests == []


async def test_a_success_clears_the_failing_clock(session, club, make_event, hub):
    _, c = club
    endpoint, _ = await add_endpoint(session, c.id)
    ep = await session.get(WebhookEndpoints, endpoint.id)
    ep.failing_since, ep.consecutive_failures = datetime.now(timezone.utc) - timedelta(days=2), 9
    event = await _event(make_event, club)
    await emit(session, c.id, "event.updated", Ref("event", event.id))
    await deliver_all(session)
    ep = await session.get(WebhookEndpoints, endpoint.id, populate_existing=True)
    assert ep.failing_since is None and ep.consecutive_failures == 0 and ep.last_success_at is not None


async def test_an_address_that_turns_private_is_refused_at_send(session, club, make_event, hub):
    from app.api.webhooks_out import sender

    _, c = club
    await add_endpoint(session, c.id, url="https://moves.example.com/x")
    real = sender.RESOLVE

    async def now_private(host, port):
        if host == "moves.example.com":
            raise net.UnsafeURL(f"{host} resolves to a private or local network address.")
        return await real(host, port)

    sender.RESOLVE = now_private
    event = await _event(make_event, club)
    await emit(session, c.id, "event.updated", Ref("event", event.id))
    await deliver_all(session)
    assert hub.requests == []
    row = await session.scalar(select(WebhookDeliveries))
    assert row.status == RETRYING and "private" in row.error


async def test_redirects_are_not_followed(session, club, make_event, hub):
    _, c = club
    await add_endpoint(session, c.id)
    hub.answer = lambda request: httpx.Response(302, headers={"Location": "https://10.0.0.1/"})
    event = await _event(make_event, club)
    await emit(session, c.id, "event.updated", Ref("event", event.id))
    await deliver_all(session)
    assert len(hub.requests) == 1
    row = await session.scalar(select(WebhookDeliveries))
    assert row.status == RETRYING and "redirect" in row.error


async def test_a_timeout_is_a_failure(session, club, make_event, hub):
    _, c = club
    await add_endpoint(session, c.id)

    def slow(request):
        raise httpx.ReadTimeout("slow", request=request)

    hub.answer = slow
    event = await _event(make_event, club)
    await emit(session, c.id, "event.updated", Ref("event", event.id))
    await deliver_all(session)
    row = await session.scalar(select(WebhookDeliveries))
    assert row.status == RETRYING and "10 seconds" in row.error


# ─── Order, the log, by hand ─────────────────────────────────────────────


async def test_changes_to_one_object_wait_for_the_older_one(session, club, make_event, hub):
    _, c = club
    endpoint, _ = await add_endpoint(session, c.id)
    event = await _event(make_event, club)
    first = await emit(session, c.id, "event.created", Ref("event", event.id))
    second = await emit(session, c.id, "event.published", Ref("event", event.id))
    await session.commit()
    await delivery.fanout(session, second)
    later = await session.scalar(select(WebhookDeliveries).where(WebhookDeliveries.event_id == second))
    assert await delivery.attempt(session, later.id, 0) is None  # waits: the older change is not out yet
    later = await session.get(WebhookDeliveries, later.id, populate_existing=True)
    assert later.waits == 1 and later.attempts == 0
    await delivery.fanout(session, first)
    earlier = await session.scalar(select(WebhookDeliveries).where(WebhookDeliveries.event_id == first))
    await delivery.attempt(session, earlier.id, 0)
    await delivery.attempt(session, later.id, 0)
    assert hub.types() == ["event.created", "event.published"]


async def test_the_delivery_log_and_sending_again(client, session, headers, club, make_event, hub):
    _, c = club
    endpoint, _ = await add_endpoint(session, c.id)
    hub.reply(500)
    event = await _event(make_event, club)
    await emit(session, c.id, "event.updated", Ref("event", event.id))
    await deliver_all(session)
    log = (await client.get(f"{BASE}/{endpoint.id}/deliveries", headers=headers)).json()
    assert len(log["data"]) == 1 and log["data"][0]["status"] == "retrying" and log["data"][0]["event_type"] == "event.updated"
    delivery_id = log["data"][0]["id"]
    detail = (await client.get(f"{BASE}/deliveries/{delivery_id}", headers=headers)).json()
    assert detail["request_body"]["type"] == "event.updated"
    hub.reply(200)
    r = await client.post(f"{BASE}/deliveries/{delivery_id}/retry", headers=headers)
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["delivery"]["status"] == "succeeded"
    assert r.json()["delivery"]["attempts"] == 2
    # A replay of one that went.
    r = await client.post(f"{BASE}/deliveries/{delivery_id}/retry", headers=headers)
    assert r.json()["ok"] is True and r.json()["delivery"]["attempts"] == 3
    assert len(hub.requests) == 3
    # Not to an endpoint turned off.
    await client.patch(f"{BASE}/{endpoint.id}", json={"enabled": False}, headers=headers)
    r = await client.post(f"{BASE}/deliveries/{delivery_id}/retry", headers=headers)
    assert r.status_code == 409 and r.json()["error_code"] == "ENDPOINT_DISABLED"
    # Filters and paging.
    page = (await client.get(f"{BASE}/{endpoint.id}/deliveries?status=succeeded&limit=1", headers=headers)).json()
    assert [d["id"] for d in page["data"]] == [delivery_id] and page["next_cursor"] is None


async def test_paging_the_log(session, club, make_event, hub):
    _, c = club
    endpoint, _ = await add_endpoint(session, c.id)
    event = await _event(make_event, club)
    for _ in range(5):
        await emit(session, c.id, "event.updated", Ref("event", event.id))
    await deliver_all(session)
    owner = service.Owner(club_id=c.id)
    seen, cursor = [], None
    while True:
        page = await service.deliveries(session, owner, endpoint.id, limit=2, cursor=cursor)
        seen += [d["id"] for d in page["data"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert len(seen) == 5 == len(set(seen))


async def test_a_test_send_answers_now_and_never_counts_as_failing(client, session, headers, club, hub):
    _, c = club
    endpoint, secret = await add_endpoint(session, c.id)
    hub.reply(404, "no such route")
    r = await client.post(f"{BASE}/{endpoint.id}/test", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is False and r.json()["delivery"]["response_status"] == 404
    ep = await session.get(WebhookEndpoints, endpoint.id, populate_existing=True)
    assert ep.failing_since is None and ep.consecutive_failures == 0
    hub.reply(200)
    r = await client.post(f"{BASE}/{endpoint.id}/test", headers=headers)
    assert r.json()["ok"] is True
    request = hub.requests[-1]
    assert json.loads(request.content)["type"] == "webhook.test"
    assert crypto.verify(secret, request.headers["MOA-Signature"], request.content)
    log = (await client.get(f"{BASE}/{endpoint.id}/deliveries", headers=headers)).json()
    assert [d["event_type"] for d in log["data"]] == ["webhook.test", "webhook.test"]


async def test_rotating_the_secret_keeps_the_old_one_signing_for_a_while(client, session, headers, club, make_event, hub):
    _, c = club
    endpoint, old = await add_endpoint(session, c.id)
    r = await client.post(f"{BASE}/{endpoint.id}/rotate-secret", headers=headers)
    assert r.status_code == 200
    new = r.json()["secret"]
    assert new != old and r.json()["previous_secret_expires_at"]
    event = await _event(make_event, club)
    await emit(session, c.id, "event.updated", Ref("event", event.id))
    await deliver_all(session)
    request = hub.requests[-1]
    assert request.headers["MOA-Signature"].count("v1=") == 2
    assert crypto.verify(new, request.headers["MOA-Signature"], request.content)
    assert crypto.verify(old, request.headers["MOA-Signature"], request.content)
    r = await client.post(f"{BASE}/{endpoint.id}/rotate-secret", json={"keep_previous_hours": 0}, headers=headers)
    newest = r.json()["secret"]
    hub.clear()
    await emit(session, c.id, "event.updated", Ref("event", event.id))
    await deliver_all(session)
    header = hub.requests[-1].headers["MOA-Signature"]
    assert header.count("v1=") == 1 and crypto.verify(newest, header, hub.requests[-1].content)
    assert not crypto.verify(new, header, hub.requests[-1].content)


async def test_turning_off_by_hand_and_back_on(client, session, headers, club, make_event, hub):
    _, c = club
    endpoint, _ = await add_endpoint(session, c.id)
    r = await client.patch(f"{BASE}/{endpoint.id}", json={"enabled": False}, headers=headers)
    assert r.json()["enabled"] is False and r.json()["disabled_reason"] == "by_owner"
    event = await _event(make_event, club)
    await emit(session, c.id, "event.updated", Ref("event", event.id))
    await deliver_all(session)
    assert hub.requests == []
    r = await client.patch(f"{BASE}/{endpoint.id}", json={"enabled": True, "events": ["event.updated"]}, headers=headers)
    assert r.json()["enabled"] is True and r.json()["events"] == ["event.updated"]
    await emit(session, c.id, "event.updated", Ref("event", event.id))
    await deliver_all(session)
    assert hub.types() == ["event.updated"]
    r = await client.delete(f"{BASE}/{endpoint.id}", headers=headers)
    assert r.status_code == 204
    assert await session.get(WebhookEndpoints, endpoint.id) is None


async def test_a_partner_endpoint_is_read_only_in_the_dashboard(client, session, headers, club, hub):
    _, c = club
    owner = service.Owner(club_id=c.id, via="partner_app", client_id="partner_acme", scopes=frozenset({"webhooks:manage"}))
    endpoint, _ = await add_endpoint(session, c.id, url="https://acme.example.com/x", owner=owner)
    listed = (await client.get(BASE, headers=headers)).json()["data"]
    assert listed[0]["owner"]["type"] == "partner_app"
    r = await client.patch(f"{BASE}/{endpoint.id}", json={"enabled": False}, headers=headers)
    assert r.status_code == 403 and r.json()["error_code"] == "PARTNER_MANAGED"


async def test_retention_deletes_what_is_older_than_30_days(session, club, make_event, hub):
    _, c = club
    await add_endpoint(session, c.id)
    event = await _event(make_event, club)
    old = await emit(session, c.id, "event.updated", Ref("event", event.id))
    recent = await emit(session, c.id, "event.published", Ref("event", event.id))
    await deliver_all(session)
    row = await session.get(WebhookEvents, old)
    row.created_at = datetime.now(timezone.utc) - timedelta(days=31)
    await session.commit()
    out = await sweep.run_sweep(session)
    assert out["deleted"] == 1
    assert [e.id for e in await events_of(session)] == [recent]
    assert await session.scalar(select(func.count()).select_from(WebhookDeliveries)) == 1


async def test_the_admins_see_failing_endpoints(client, session, club, make_admin, mint_token, make_event, hub):
    _, c = club
    endpoint, _ = await add_endpoint(session, c.id)
    hub.reply(500)
    event = await _event(make_event, club)
    await emit(session, c.id, "event.updated", Ref("event", event.id))
    await deliver_all(session)
    admin = await make_admin()
    h = auth_header(await mint_token(admin))
    health = (await client.get("/api/v1/admin/integrations/webhooks/health", headers=h)).json()
    assert health["endpoints"]["failing_now"] == 1
    assert health["failing_endpoints"][0]["id"] == str(endpoint.id) and health["failing_endpoints"][0]["club_name"] == "Hook Club"
    assert sum(d["deliveries"] for d in health["per_day"]) == 1
    r = await client.patch(f"/api/v1/admin/integrations/webhooks/endpoints/{endpoint.id}", json={"enabled": False}, headers=h)
    assert r.json()["enabled"] is False and r.json()["disabled_reason"] == "by_admin"
    # Not for a club.
    club_h = auth_header(await mint_token(club[0]))
    assert (await client.get("/api/v1/admin/integrations/webhooks/health", headers=club_h)).status_code in (401, 403)


# ─── The partner API's routes (mounted by the partner router) ────────────


def _partner_app(caller):
    from fastapi import FastAPI

    from app.api.webhooks_out.partner import make_router
    from app.db.core import get_session

    def require_scope(name):
        async def dep():
            if name not in caller.scopes:
                from fastapi import HTTPException

                raise HTTPException(403, detail={"code": "FORBIDDEN_SCOPE"})
            return caller
        return dep

    app = FastAPI()
    app.include_router(make_router(require_scope), prefix="/partner/v1")
    return app, get_session


class Caller:
    def __init__(self, club_id, client_id, client_type, scopes, livemode=True):
        self.club_id, self.client_id, self.client_type, self.scopes, self.livemode = club_id, client_id, client_type, scopes, livemode


async def test_partner_routes_keep_each_client_to_its_own(session, club, hub):
    from httpx import ASGITransport, AsyncClient

    _, c = club
    await add_endpoint(session, c.id, url="https://dash.example.com/x")

    async def run(caller):
        app, get_session = _partner_app(caller)

        async def same():
            yield session

        app.dependency_overrides[get_session] = same
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://partner")

    partner = Caller(c.id, "partner_acme", "partner_app", {"webhooks:manage"})
    async with await run(partner) as p:
        r = await p.post("/partner/v1/webhook-endpoints", json={"url": "https://acme.example.com/x", "events": ["registration.created"]})
        assert r.status_code == 201, r.text
        assert r.json()["secret"].startswith("whsec_") and r.json()["owner"] == {"type": "partner_app", "client_id": "partner_acme", "partner_app_id": None}
        listed = (await p.get("/partner/v1/webhook-endpoints")).json()["data"]
        assert [e["url"] for e in listed] == ["https://acme.example.com/x"]
        # A partner cannot ask for attendee details by the setting.
        r = await p.post("/partner/v1/webhook-endpoints", json={"url": "https://acme.example.com/y", "share_attendee_details": True, "dpdp_confirmed": True})
        assert r.status_code == 422
    key = Caller(c.id, "key_live_1", "api_key", {"webhooks:manage"}, livemode=False)
    async with await run(key) as k:
        listed = (await k.get("/partner/v1/webhook-endpoints")).json()["data"]
        assert [e["url"] for e in listed] == ["https://dash.example.com/x"]
        r = await k.post("/partner/v1/webhook-endpoints", json={"url": "https://erp.example.com/x"})
        assert r.status_code == 201 and r.json()["livemode"] is False and r.json()["owner"]["client_id"] == "key_live_1"
        r = await k.post("/partner/v1/webhook-endpoints", json={"url": "https://erp.example.com/y", "livemode": True})
        assert r.status_code == 422
        # Attendee details need the key's own attendees:read.
        r = await k.post("/partner/v1/webhook-endpoints", json={"url": "https://erp.example.com/z", "share_attendee_details": True, "dpdp_confirmed": True})
        assert r.status_code == 403
    noscope = Caller(c.id, "key_2", "api_key", {"events:read"})
    async with await run(noscope) as n:
        assert (await n.get("/partner/v1/webhook-endpoints")).status_code == 403


# ─── Pieces ──────────────────────────────────────────────────────────────


def test_surface_of_a_request():
    from app.api.webhooks_out.origin import surface

    assert surface("/api/v1/events/1", {"x-app-version": "1.2.1"}) == "app"
    assert surface("/api/v1/events/1", {"origin": settings.PUBLIC_WEB_URL}) == "dashboard"
    assert surface("/api/v1/events/1", {"origin": settings.PUBLIC_ADMIN_URL}) == "dashboard"
    assert surface("/api/v1/events/1", {"origin": "https://myotherapp.in"}) == "website"
    assert surface("/api/v1/events/1", {"referer": "https://hack-club.myotherapp.in/events/x"}) == "website"
    assert surface("/api/v1/events/1", {"authorization": "Bearer abc"}) == "app"
    assert surface("/api/v1/payments/webhook", {}) == "system"
    assert surface("/mcp", {}) == "dashboard"
    assert surface("/partner/v1/events", {"origin": "https://x.example"}) is None


@pytest.mark.parametrize("ip,public", [
    ("93.184.216.34", True), ("10.0.0.1", False), ("172.16.0.1", False), ("192.168.1.1", False), ("127.0.0.1", False),
    ("169.254.169.254", False), ("100.64.0.1", False), ("0.0.0.0", False), ("224.0.0.1", False), ("::1", False),
    ("fe80::1", False), ("fc00::1", False), ("::ffff:127.0.0.1", False), ("2606:4700:4700::1111", True),
])
def test_public_addresses(ip, public):
    assert net.public_ip(ip) is public


async def test_resolve_refuses_one_private_answer_among_public_ones(monkeypatch):
    import socket

    def fake(host, port, type=None):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.7", port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    with pytest.raises(net.UnsafeURL):
        await net.resolve_public("mixed.example.com", 443)


def test_previous_names_the_fields_that_changed():
    before = {"name": "A", "updated_at": "1", "poster_url": "https://s/1?sig=a", "_poster_url": "k1", "capacity": 10}
    after = {"name": "B", "updated_at": "2", "poster_url": "https://s/1?sig=b", "_poster_url": "k1", "capacity": 10}
    assert objects.diff(before, after) == {"name": "A"}
    after2 = dict(after, _poster_url="k2")
    assert objects.diff(before, after2) == {"name": "A", "poster_url": "https://s/1?sig=a"}
