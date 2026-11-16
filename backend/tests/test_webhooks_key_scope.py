"""What a club API key (or a partner app) can see and do with webhook
endpoints, and how long people's details stay in what webhooks keep:

- a key sees only its own endpoints (the ones it added, and the ones the
  dashboard linked to it), in its own mode; the dashboard sees them all;
- the delivery log leaves people's details out for a caller without
  attendees:read, whatever the endpoint was sent;
- a new address asks again before people's details go there;
- a revoked or expired key's endpoints are turned off and stay off;
- the event's data wipe takes people's details out of the kept changes.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.api.closeout import service as closeout
from app.api.webhooks_out import delivery, service
from app.api.webhooks_out.emit import Ref, emit
from app.api.webhooks_out.models import FAILED, RETRYING, WebhookDeliveries, WebhookEndpoints, WebhookEvents
from tests.webhooks_helpers import add_endpoint, deliver_all, hub  # noqa: F401

KEY = "key_erp"
MANAGE = ("webhooks:manage",)
READER = ("webhooks:manage", "attendees:read")


@pytest.fixture
async def club(make_club, hub):
    user, club = await make_club(name="Scope Club", email="scope@club.example")
    hub.allocated.add(club.id)
    return user, club


def key(club_id: int, client_id: str = KEY, *, livemode: bool = True, scopes=MANAGE) -> service.Owner:
    return service.Owner(club_id=club_id, via="api_key", client_id=client_id, livemode=livemode, scopes=frozenset(scopes),
                         name="ERP key")


def partner(club_id: int, *, livemode: bool = True) -> service.Owner:
    return service.Owner(club_id=club_id, via="partner_app", client_id="partner_acme", livemode=livemode,
                         scopes=frozenset(MANAGE), name="Acme")


async def _event(make_event, club, **kw):
    user, c = club
    return await make_event(club=c, created_by=user, name=kw.pop("name", "Scope Night"), **kw)


async def _refused(call) -> service.WebhookError:
    with pytest.raises(service.WebhookError) as caught:
        await call
    return caught.value


# ─── Seeing endpoints ────────────────────────────────────────────────────


async def test_a_key_sees_only_its_own_endpoints_in_its_own_mode(session, club, make_event, hub):
    _, c = club
    dash, _ = await add_endpoint(session, c.id, url="https://dash.example.com/x")
    linked, _ = await add_endpoint(session, c.id, url="https://linked.example.com/x", client_id=KEY)
    linked_test, _ = await add_endpoint(session, c.id, url="https://linked-test.example.com/x", client_id=KEY, livemode=False)
    own, _ = await add_endpoint(session, c.id, url="https://own.example.com/x", owner=key(c.id))
    other, _ = await add_endpoint(session, c.id, url="https://other.example.com/x", owner=key(c.id, "key_other"))
    acme, _ = await add_endpoint(session, c.id, url="https://acme.example.com/x", owner=partner(c.id))

    mine = key(c.id)
    assert sorted(e.url for e in await service.listing(session, mine)) == [linked.url, own.url]
    # The same client in the other mode sees only that mode's.
    assert [e.url for e in await service.listing(session, key(c.id, livemode=False))] == [linked_test.url]
    # The dashboard sees every one of the club's.
    assert len(await service.listing(session, service.Owner(club_id=c.id))) == 6
    # A partner, its own in its own mode.
    assert [e.url for e in await service.listing(session, partner(c.id))] == [acme.url]
    assert await service.listing(session, partner(c.id, livemode=False)) == []

    for hidden in (dash, linked_test, other, acme):
        assert (await _refused(service.get(session, mine, hidden.id))).status == 404
        assert (await _refused(service.update(session, mine, hidden.id, {"enabled": False}))).status == 404
        assert (await _refused(service.rotate(session, mine, hidden.id))).status == 404
        assert (await _refused(service.delete(session, mine, hidden.id))).status == 404
        assert (await _refused(service.deliveries(session, mine, hidden.id))).status == 404

    # Nor another endpoint's deliveries.
    event = await _event(make_event, club)
    await emit(session, c.id, "event.updated", Ref("event", event.id))
    await deliver_all(session)
    theirs = await session.scalar(select(WebhookDeliveries).where(WebhookDeliveries.endpoint_id == other.id))
    assert theirs is not None
    assert (await _refused(service.delivery_detail(session, mine, theirs.id))).status == 404
    assert (await _refused(service.retry(session, mine, theirs.id))).status == 404
    ours = await session.scalar(select(WebhookDeliveries).where(WebhookDeliveries.endpoint_id == own.id))
    assert (await service.delivery_detail(session, mine, ours.id))["request_body"]["type"] == "event.updated"


async def test_a_caller_without_a_client_id_sees_nothing(session, club, hub):
    _, c = club
    await add_endpoint(session, c.id, url="https://dash.example.com/x")
    nobody = service.Owner(club_id=c.id, via="api_key", client_id=None, livemode=True, scopes=frozenset(MANAGE))
    assert await service.listing(session, nobody) == []


# ─── People's details in the log ─────────────────────────────────────────


async def test_the_log_leaves_out_people_s_details_without_attendees_read(session, club, make_event, make_registration, make_user, hub):
    _, c = club
    shared, _ = await add_endpoint(session, c.id, url="https://dpdp.example.com/x", client_id=KEY,
                                   share_attendee_details=True, dpdp_confirmed=True)
    event = await _event(make_event, club)
    person = await make_user(email="asha@example.com")
    reg = await make_registration(event=event, user=person, phone="9876543210", additional_details={"college": "CET"})
    await emit(session, c.id, "registration.updated", Ref("registration", reg.id), previous={"answers": {"college": "NIT"}})
    await deliver_all(session)
    # The endpoint was sent them (the club ticked the box for it).
    sent = hub.bodies()[0]["data"]
    assert sent["object"]["person"]["email"] == "asha@example.com" and sent["previous"] == {"answers": {"college": "NIT"}}

    row = await session.scalar(select(WebhookDeliveries).where(WebhookDeliveries.endpoint_id == shared.id))
    blind = (await service.delivery_detail(session, key(c.id), row.id))["request_body"]["data"]
    assert "person" not in blind["object"] and "answers" not in blind["object"] and "previous" not in blind
    assert blind["object"]["id"] == str(reg.id) and blind["object"]["ticket"]["code"] == reg.ticket_id
    for owner in (key(c.id, scopes=READER), service.Owner(club_id=c.id)):
        seen = (await service.delivery_detail(session, owner, row.id))["request_body"]["data"]
        assert seen["object"]["person"] == {"name": person.full_name, "email": "asha@example.com", "phone": "9876543210"}
        assert seen["object"]["answers"] == {"college": "CET"} and seen["previous"] == {"answers": {"college": "NIT"}}


async def test_the_log_never_shows_more_than_was_sent(session, club, make_event, make_registration, make_user, hub):
    _, c = club
    plain, _ = await add_endpoint(session, c.id, url="https://plain.example.com/x", owner=key(c.id, scopes=READER))
    event = await _event(make_event, club)
    reg = await make_registration(event=event, user=await make_user(email="ravi@example.com"))
    await emit(session, c.id, "registration.created", Ref("registration", reg.id))
    await deliver_all(session)
    row = await session.scalar(select(WebhookDeliveries).where(WebhookDeliveries.endpoint_id == plain.id))
    body = (await service.delivery_detail(session, key(c.id, scopes=READER), row.id))["request_body"]["data"]["object"]
    assert "person" not in body and "answers" not in body


# ─── A new address ───────────────────────────────────────────────────────


async def test_a_new_address_asks_again_before_people_s_details_go_there(session, club, hub):
    _, c = club
    ep, _ = await add_endpoint(session, c.id, url="https://dpdp.example.com/x", client_id=KEY,
                               share_attendee_details=True, dpdp_confirmed=True)
    first_confirmed = ep.dpdp_confirmed_at

    # A key that may not read people's details cannot keep them going to a new address.
    refused = await _refused(service.update(session, key(c.id), ep.id, {"url": "https://elsewhere.example.com/x", "dpdp_confirmed": True}))
    assert refused.status == 403 and refused.code == "FORBIDDEN_SCOPE"
    # Refused before anything changed.
    ep = await session.get(WebhookEndpoints, ep.id, populate_existing=True)
    assert ep.url == "https://dpdp.example.com/x" and ep.share_attendee_details is True

    # Without confirming, the address changes and the details stop.
    ep = await service.update(session, key(c.id), ep.id, {"url": "https://elsewhere.example.com/x"})
    await session.commit()
    assert ep.share_attendee_details is False and ep.dpdp_confirmed_at is None and ep.dpdp_confirmed_by is None

    # Ticked again, then moved again with the statement confirmed for the new
    # address by a key that reads the details itself: they keep going.
    ep = await service.update(session, service.Owner(club_id=c.id, name="Owner"), ep.id,
                              {"share_attendee_details": True, "dpdp_confirmed": True})
    await session.commit()
    ep = await service.update(session, key(c.id, scopes=READER), ep.id, {"url": "https://third.example.com/x", "dpdp_confirmed": True})
    await session.commit()
    assert ep.url == "https://third.example.com/x" and ep.share_attendee_details is True
    assert ep.dpdp_confirmed_by == "ERP key" and ep.dpdp_confirmed_at > first_confirmed

    # The dashboard too, the same way.
    ep = await service.update(session, service.Owner(club_id=c.id, name="Owner"), ep.id,
                              {"url": "https://fourth.example.com/x", "dpdp_confirmed": True})
    await session.commit()
    assert ep.share_attendee_details is True and ep.dpdp_confirmed_by == "Owner"


# ─── Revoked and expired keys ────────────────────────────────────────────


async def test_an_expired_key_s_endpoints_are_turned_off_at_the_next_send(session, club, make_event, hub):
    _, c = club
    own, _ = await add_endpoint(session, c.id, url="https://own.example.com/x", owner=key(c.id))
    linked, _ = await add_endpoint(session, c.id, url="https://linked.example.com/x", client_id=KEY)
    hub.dead_keys.add(KEY)
    event = await _event(make_event, club)
    await emit(session, c.id, "event.updated", Ref("event", event.id))
    await deliver_all(session)
    # The club's own endpoint linked to the key is the club's: still sent to.
    assert [r.headers["Host"] for r in hub.requests] == ["linked.example.com"]
    own = await session.get(WebhookEndpoints, own.id, populate_existing=True)
    assert own.enabled is False and own.disabled_reason == "by_owner"
    # And it cannot be turned back on.
    refused = await _refused(service.update(session, service.Owner(club_id=c.id), own.id, {"enabled": True}))
    assert refused.status == 409 and refused.code == "KEY_REVOKED"


async def test_a_retry_after_the_key_ended_is_not_sent(session, club, make_event, hub):
    _, c = club
    own, _ = await add_endpoint(session, c.id, url="https://own.example.com/x", owner=key(c.id))
    hub.reply(500)
    event = await _event(make_event, club)
    await emit(session, c.id, "event.updated", Ref("event", event.id))
    await deliver_all(session)
    row = await session.scalar(select(WebhookDeliveries).where(WebhookDeliveries.endpoint_id == own.id))
    assert row.status == RETRYING and row.attempts == 1
    hub.clear()
    hub.dead_keys.add(KEY)
    assert await delivery.attempt(session, row.id, 1) is None
    assert hub.requests == []
    row = await session.get(WebhookDeliveries, row.id, populate_existing=True)
    own = await session.get(WebhookEndpoints, own.id, populate_existing=True)
    assert row.status == FAILED and own.enabled is False


# ─── The data wipe ───────────────────────────────────────────────────────


async def test_the_data_wipe_takes_people_s_details_out_of_kept_changes(session, club, make_event, make_registration, make_user, hub):
    _, c = club
    await add_endpoint(session, c.id, url="https://dpdp.example.com/x", share_attendee_details=True, dpdp_confirmed=True)
    event = await _event(make_event, club)
    other_event = await _event(make_event, club, name="Another Night")
    asha = await make_registration(event=event, user=await make_user(email="asha@example.com"), phone="9876543210",
                                   additional_details={"college": "CET"})
    gone = await make_registration(event=event, user=await make_user(email="gone@example.com"), is_deleted=True)
    kept = await make_registration(event=other_event, user=await make_user(email="kept@example.com"), phone="9000000000")
    await emit(session, c.id, "registration.created", Ref("registration", asha.id))
    await emit(session, c.id, "registration.updated", Ref("registration", asha.id), previous={"answers": {"college": "NIT"}})
    await emit(session, c.id, "certificate.issued", Ref("certificate", asha.id))
    await emit(session, c.id, "registration.cancelled", Ref("registration", gone.id))
    await emit(session, c.id, "registration.created", Ref("registration", kept.id))
    await emit(session, c.id, "event.updated", Ref("event", event.id))
    await deliver_all(session)
    before = {(e.type, e.object_id): e for e in await session.scalars(select(WebhookEvents))}
    assert before[("registration.created", str(asha.id))].data["person"]["email"] == "asha@example.com"
    assert before[("certificate.issued", str(asha.id))].data["name"]

    job = await closeout.ensure_job(session, event)
    await closeout.scrub(session, event, job)

    after = {(e.type, e.object_id): e for e in await session.scalars(select(WebhookEvents).execution_options(populate_existing=True))}
    for kind in ("registration.created", "registration.updated", "certificate.issued"):
        data = after[(kind, str(asha.id))].data
        assert "person" not in data and "answers" not in data and "name" not in data, kind
        assert (data["registration_id"] if kind == "certificate.issued" else data["id"]) == str(asha.id)
    assert after[("registration.updated", str(asha.id))].previous in (None, {})
    assert "person" not in after[("registration.cancelled", str(gone.id))].data
    # Another event's people, and the event itself, are untouched.
    assert after[("registration.created", str(kept.id))].data["person"]["email"] == "kept@example.com"
    assert after[("event.updated", str(event.id))].data["id"] == str(event.id)
