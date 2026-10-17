"""Complimentary plans: a plan MyOtherApp gives a club for nothing.

An admin grants it (to any club, subscribed or not), it is worth nothing in
MRR and never shows as revenue, Razorpay is stopped first so the club is
never billed while it has it, and it ends on time: the club is told a week
ahead and when it ends, and goes back to what it had or to Free.

Razorpay is a fake throughout (FakeRazorpay of test_club_billing), mail is
captured by the `mail` fixture of test_billing_alerts.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from tests.conftest import auth_header
from tests.test_billing_alerts import _alerts, _day, _hook, _paid, mail  # noqa: F401  (mail is a fixture)
from tests.test_club_billing import (  # noqa: F401  (razorpay and shop are fixtures)
    FakeRazorpay,
    _row,
    razorpay,
    shop,
)

API = "/api/v1"
DAY = timedelta(days=1)


def _now():
    return datetime.now(timezone.utc)


async def _admin(make_admin, mint_token, email="comp-admin@example.com", role=None):
    admin = await make_admin(email=email, role=role)
    return admin, auth_header(await mint_token(admin))


async def _grant(client, header, club_id, plan_id, **body):
    payload = {"plan_id": plan_id, "cycle": "monthly", "reason": "Sponsored by the dean's office", **body}
    if isinstance(payload.get("ends_at"), datetime):
        payload["ends_at"] = payload["ends_at"].isoformat()
    return await client.put(f"{API}/admin/subscriptions/club/{club_id}/plan", json=payload, headers=header)


async def _charges(session):
    from app.api.subscriptions.models import SubscriptionCharges

    return int(await session.scalar(select(func.count(SubscriptionCharges.id))) or 0)


# ─── 1. Granting ─────────────────────────────────────────────────────────


async def test_a_club_with_no_subscription_can_be_given_a_plan_free(
    client, session, shop, make_admin, mint_token, razorpay
):
    from app.api.audit.models import AdminAuditLogs
    from app.api.finance.models import ClubExpenses

    admin, header = await _admin(make_admin, mint_token)
    assert await _row(session, shop["club"].id) is None  # a club on Free has no row at all
    ends = _now() + 60 * DAY

    response = await _grant(client, header, shop["club"].id, shop["pro"].id, ends_at=ends)

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["status"], body["complimentary"], body["mrr_paise"], body["entitled"]) == ("active", True, 0, True)
    assert body["comp_reason"] == "Sponsored by the dean's office"
    assert body["granted_by"] == {"id": admin.id, "name": admin.full_name}
    assert abs(datetime.fromisoformat(body["comp_ends_at"]) - ends) < timedelta(seconds=1)
    assert body["current_period_end"] == body["comp_ends_at"]
    assert body["razorpay_subscription_id"] is None
    assert razorpay.calls == []  # nothing at Razorpay to stop, and nothing charged

    # The club has the plan.
    ent = (await client.get(f"{API}/clubs/entitlements", headers=shop["h"])).json()
    assert (ent["plan_key"], ent["complimentary"]) == ("pro", True)
    assert ent["comp_ends_at"] is not None and "comp_reason" not in ent

    # Recorded and announced as a free grant.
    [log] = (await session.execute(
        select(AdminAuditLogs).where(AdminAuditLogs.action == "subscription.comp_grant")
    )).scalars().all()
    assert log.details["reason"] == "Sponsored by the dean's office" and log.details["plan"] == "pro"
    [alert] = await _alerts(session, "plan_assigned")
    assert alert.data["complimentary"] is True and alert.data["plan_name"] == "Pro"
    assert alert.data["ends_at"] is not None

    # Worth nothing, written nowhere money is: no charge, nothing in the
    # club's own Finance ledger.
    totals = (await client.get(f"{API}/admin/subscriptions/totals", headers=header)).json()
    assert (totals["mrr_paise"], totals["arr_paise"], totals["paying"], totals["complimentary"]) == (0, 0, 0, 1)
    assert totals["active_subscriptions"] == 0
    assert await _charges(session) == 0
    assert await session.scalar(select(func.count(ClubExpenses.id))) == 0


async def test_a_comp_with_no_end_runs_until_revoked(client, session, shop, make_admin, mint_token):
    _, header = await _admin(make_admin, mint_token)
    response = await _grant(client, header, shop["club"].id, shop["growth"].id, ends_at=None)
    assert response.status_code == 200, response.text
    assert response.json()["comp_ends_at"] is None and response.json()["entitled"] is True


async def test_a_comp_needs_a_reason_and_a_future_end(client, session, shop, make_admin, mint_token):
    _, header = await _admin(make_admin, mint_token)
    club, plan = shop["club"].id, shop["pro"].id
    url = f"{API}/admin/subscriptions/club/{club}/plan"

    # complimentary is the default: no reason, no grant.
    assert (await client.put(url, json={"plan_id": plan}, headers=header)).status_code == 422
    assert (await _grant(client, header, club, plan, reason="   ")).status_code == 422
    assert (await _grant(client, header, club, plan, reason="x" * 201)).status_code == 422
    assert (await _grant(client, header, club, plan, ends_at=_now() - DAY)).status_code == 422
    # An end is only for a comp.
    paid = await client.put(url, json={"plan_id": plan, "complimentary": False,
                                        "ends_at": (_now() + DAY).isoformat()}, headers=header)
    assert paid.status_code == 422
    # The web's own shape: an India end-of-day, no reason key when empty.
    ok = await client.put(url, json={"plan_id": plan, "cycle": "yearly", "complimentary": True,
                                      "reason": "Hackathon partner",
                                      "ends_at": f"{(_now() + 10 * DAY).date()}T23:59:59+05:30",
                                      "cancel_razorpay": False}, headers=header)
    assert ok.status_code == 200, ok.text
    assert await _row(session, club) is not None


async def test_granting_needs_the_money_permission(client, shop, make_admin, mint_token):
    from app.api.users.models import AdminRoles

    _, header = await _admin(make_admin, mint_token, email="viewer@example.com", role=AdminRoles.viewer)
    response = await _grant(client, header, shop["club"].id, shop["pro"].id)
    assert response.status_code == 403, response.text


async def test_a_deal_by_hand_still_counts_at_the_plan_price(client, session, shop, make_admin, mint_token):
    _, header = await _admin(make_admin, mint_token)
    response = await _grant(client, header, shop["club"].id, shop["growth"].id, complimentary=False, reason=None)
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["complimentary"], body["mrr_paise"], body["comp_reason"]) == (False, 69900, None)
    totals = (await client.get(f"{API}/admin/subscriptions/totals", headers=header)).json()
    assert (totals["mrr_paise"], totals["paying"], totals["complimentary"]) == (69900, 1, 0)
    [alert] = await _alerts(session, "plan_assigned")
    assert alert.data["complimentary"] is False and alert.data["plan_name"] == "Growth"


# ─── 2. Never on top of a live Razorpay subscription ─────────────────────


async def test_a_club_paying_through_razorpay_is_refused_unless_it_is_stopped(
    client, session, shop, make_admin, mint_token, razorpay
):
    await _paid(session, shop)  # active, Razorpay id sub_paid
    _, header = await _admin(make_admin, mint_token)

    refused = await _grant(client, header, shop["club"].id, shop["pro"].id)
    assert refused.status_code == 409, refused.text
    assert refused.json()["error_code"] == "LIVE_SUBSCRIPTION"
    row = await _row(session, shop["club"].id)
    assert (row.complimentary, row.razorpay_subscription_id, row.status.value) == (False, "sub_paid", "active")
    assert razorpay.calls == []

    granted = await _grant(client, header, shop["club"].id, shop["pro"].id, cancel_razorpay=True)
    assert granted.status_code == 200, granted.text
    # Stopped now, not at the end of the cycle.
    assert razorpay.of("subscription", "cancel") == [("sub_paid", {"cancel_at_cycle_end": 0})]
    row = await _row(session, shop["club"].id)
    assert (row.complimentary, row.razorpay_subscription_id, row.mrr_paise) == (True, None, 0)
    # What the club had is kept, as a checkout keeps it.
    assert row.previous_state["razorpay_subscription_id"] == "sub_paid"
    assert row.previous_state["plan_id"] == shop["growth"].id
    [alert] = await _alerts(session, "plan_assigned")
    assert alert.data["razorpay_cancelled"] == "sub_paid"


async def test_razorpay_confirming_the_cancel_later_leaves_the_comp_alone(
    client, session, shop, make_admin, mint_token, razorpay
):
    await _paid(session, shop)
    _, header = await _admin(make_admin, mint_token)
    assert (await _grant(client, header, shop["club"].id, shop["pro"].id, cancel_razorpay=True)).status_code == 200

    await _hook(session, "subscription.cancelled", "sub_paid", notes={"club_id": str(shop["club"].id)})
    # A late charge on the old subscription is not adopted over the comp either.
    await _hook(session, "subscription.charged", "sub_paid", notes={"club_id": str(shop["club"].id)},
                payment={"id": "pay_late", "amount": 69900, "created_at": int(_now().timestamp())})

    row = await _row(session, shop["club"].id)
    assert (row.status.value, row.complimentary, row.plan_id) == ("active", True, shop["pro"].id)
    assert await _charges(session) == 0


async def test_when_razorpay_will_not_stop_nothing_is_granted(
    client, session, shop, make_admin, mint_token, razorpay
):
    await _paid(session, shop)
    _, header = await _admin(make_admin, mint_token)

    def refuse(sub_id, data):
        raise RuntimeError("Razorpay is down")

    razorpay.handlers[("subscription", "cancel")] = refuse
    razorpay.handlers[("subscription", "fetch")] = lambda sub_id: {"id": sub_id, "status": "active"}
    response = await _grant(client, header, shop["club"].id, shop["pro"].id, cancel_razorpay=True)
    assert response.status_code == 502, response.text
    assert response.json()["error_code"] == "RAZORPAY_ERROR"
    row = await _row(session, shop["club"].id)
    assert (row.complimentary, row.razorpay_subscription_id) == (False, "sub_paid")

    # Already over at Razorpay (its refusal says so): granted.
    razorpay.handlers[("subscription", "fetch")] = lambda sub_id: {"id": sub_id, "status": "cancelled"}
    response = await _grant(client, header, shop["club"].id, shop["pro"].id, cancel_razorpay=True)
    assert response.status_code == 200, response.text


async def test_an_open_checkout_is_closed_before_the_grant(client, session, shop, make_admin, mint_token, razorpay):
    started = await client.post(f"{API}/clubs/subscription/start", json={"plan_key": "growth"}, headers=shop["h"])
    assert started.status_code == 200, started.text
    _, header = await _admin(make_admin, mint_token)

    response = await _grant(client, header, shop["club"].id, shop["pro"].id)

    assert response.status_code == 200, response.text
    assert razorpay.of("subscription", "cancel") == [("sub_new1", {"cancel_at_cycle_end": 0})]
    row = await _row(session, shop["club"].id)
    assert (row.status.value, row.complimentary, row.pending_since) == ("active", True, None)


# ─── 3. What the club sees, and may not do ───────────────────────────────


async def test_the_club_sees_that_it_is_free_but_never_why(client, session, shop, make_admin, mint_token):
    _, header = await _admin(make_admin, mint_token)
    await _grant(client, header, shop["club"].id, shop["pro"].id, reason="Board member's club", ends_at=_now() + 30 * DAY)

    mine = await client.get(f"{API}/clubs/subscription", headers=shop["h"])
    assert mine.status_code == 200, mine.text
    body = mine.json()
    assert body["complimentary"] is True and body["comp_ends_at"] is not None
    assert "comp_reason" not in body and "granted_by" not in body
    assert "Board member" not in mine.text
    ent = await client.get(f"{API}/clubs/entitlements", headers=shop["h"])
    assert "Board member" not in ent.text and "comp_reason" not in ent.json()


async def test_a_comped_club_cannot_start_a_checkout_until_the_comp_ends(
    client, session, shop, make_admin, mint_token, razorpay, mail
):
    _, header = await _admin(make_admin, mint_token)
    await _grant(client, header, shop["club"].id, shop["pro"].id, ends_at=_now() + 30 * DAY)

    refused = await client.post(f"{API}/clubs/subscription/start", json={"plan_key": "growth"}, headers=shop["h"])
    assert refused.status_code == 409, refused.text
    assert refused.json()["error_code"] == "COMPLIMENTARY_PLAN"
    assert refused.json()["message"] == "Your plan is provided by MyOtherApp — talk to us to change it."
    assert razorpay.of("subscription", "create") == []

    # The comp's end passes; the sweep has not run yet. Checkout works, and
    # the comp is put away on the way (with its one "has ended" mail).
    row = await _row(session, shop["club"].id)
    row.comp_ends_at = _now() - timedelta(seconds=60)
    await session.commit()
    started = await client.post(f"{API}/clubs/subscription/start", json={"plan_key": "growth"}, headers=shop["h"])
    assert started.status_code == 200, started.text
    row = await _row(session, shop["club"].id)
    assert (row.status.value, row.complimentary, row.comp_reason) == ("pending", False, None)
    await mail.drain()
    assert [m["subject"] for m in mail.to("billing-owner@example.com")] == ["Your complimentary Pro has ended"]


# ─── 4. Ending ───────────────────────────────────────────────────────────


async def test_an_ended_comp_stops_counting_before_the_sweep_and_the_sweep_moves_it_to_free(
    client, session, shop, make_admin, mint_token, mail
):
    from app.api.subscriptions import service, sweep

    _, header = await _admin(make_admin, mint_token)
    await _grant(client, header, shop["club"].id, shop["pro"].id, ends_at=_now() + 30 * DAY)
    row = await _row(session, shop["club"].id)
    row.comp_ends_at = _now() - timedelta(seconds=60)
    await session.commit()

    # Not honoured the moment it ends, whatever the sweep has done.
    row = await _row(session, shop["club"].id)
    assert service.is_entitled(row) is False
    assert (await service.resolve_entitlements(session, shop["club"].id)).plan.key == "free"

    done = await sweep.sweep(session)
    assert done["comp_ended"] == 1
    row = await _row(session, shop["club"].id)
    assert (row.status.value, row.complimentary, row.comp_ends_at, row.granted_by) == ("cancelled", False, None, None)
    [alert] = await _alerts(session, "comp_ended")
    assert alert.data["outcome"] == "cancelled" and alert.data["now_on"] == "free"

    await mail.drain()
    [ended] = mail.to("billing-owner@example.com")
    assert ended["subject"] == "Your complimentary Pro has ended"
    assert "the Free plan" in ended["text"]
    # Once.
    assert (await sweep.sweep(session))["comp_ended"] == 0
    await mail.drain()
    assert len(mail.to("billing-owner@example.com")) == 1


async def test_an_ended_comp_gives_back_the_deal_it_replaced(client, session, shop, make_admin, mint_token, mail):
    from app.api.subscriptions import service, sweep

    _, header = await _admin(make_admin, mint_token)
    deal = await _grant(client, header, shop["club"].id, shop["growth"].id, complimentary=False, reason=None)
    assert deal.status_code == 200
    await _grant(client, header, shop["club"].id, shop["pro"].id, ends_at=_now() + 10 * DAY)
    # Granted again for longer: what it gives back is still the deal, not the first comp.
    await _grant(client, header, shop["club"].id, shop["pro"].id, ends_at=_now() + 20 * DAY)
    row = await _row(session, shop["club"].id)
    assert row.previous_state["plan_id"] == shop["growth"].id
    row.comp_ends_at = _now() - timedelta(seconds=60)
    await session.commit()

    # Before the sweep, the deal holds already.
    assert (await service.resolve_entitlements(session, shop["club"].id)).plan.key == "growth"
    assert (await sweep.sweep(session))["comp_ended"] == 1
    row = await _row(session, shop["club"].id)
    assert (row.plan_id, row.status.value, row.complimentary, row.mrr_paise) == (shop["growth"].id, "active", False, 69900)
    [alert] = await _alerts(session, "comp_ended")
    assert alert.data["outcome"] == "restored" and alert.data["now_on"] == "growth"
    await mail.drain()
    [ended] = mail.to("billing-owner@example.com")
    assert "Growth" in ended["text"]


async def test_a_week_before_the_end_the_club_and_the_admins_hear_once(
    client, session, shop, make_admin, mint_token, mail
):
    from app.api.subscriptions import sweep

    _, header = await _admin(make_admin, mint_token)
    ends = _now() + 20 * DAY
    await _grant(client, header, shop["club"].id, shop["pro"].id, ends_at=ends)

    assert (await sweep.sweep(session))["comp_reminded"] == 0  # three weeks out
    assert (await sweep.sweep(session, now=ends - 6 * DAY))["comp_reminded"] == 1
    assert (await sweep.sweep(session, now=ends - 5 * DAY))["comp_reminded"] == 0

    [alert] = await _alerts(session, "comp_ending")
    assert alert.data["complimentary"] is True
    await mail.drain()
    [reminder] = mail.to("billing-owner@example.com")
    assert reminder["subject"] == f"Your complimentary Pro ends on {_day(ends)}"
    assert "the Free plan" in reminder["text"]
    # And a new end is a new reminder.
    await _grant(client, header, shop["club"].id, shop["pro"].id, ends_at=ends + 30 * DAY)
    assert (await _row(session, shop["club"].id)).comp_reminded_at is None


async def test_the_admin_cancel_revokes_a_comp(client, session, shop, make_admin, mint_token):
    from app.api.subscriptions import service

    _, header = await _admin(make_admin, mint_token)
    sub_id = (await _grant(client, header, shop["club"].id, shop["pro"].id, ends_at=_now() + 30 * DAY)).json()["id"]

    response = await client.post(f"{API}/admin/subscriptions/{sub_id}/cancel", json={"at_period_end": True},
                                 headers=header)

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["status"], body["complimentary"], body["entitled"]) == ("cancelled", False, False)
    assert (await service.resolve_entitlements(session, shop["club"].id)).plan.key == "free"
    [alert] = await _alerts(session, "subscription_cancelled")
    assert alert.data["complimentary"] is True and "revoked" in alert.title


# ─── 5. The admin's list ─────────────────────────────────────────────────


async def test_the_admin_list_shows_and_filters_comps(client, session, shop, make_club, make_admin, mint_token):
    admin, header = await _admin(make_admin, mint_token)
    await _grant(client, header, shop["club"].id, shop["pro"].id, reason="Sponsored", ends_at=_now() + 9 * DAY)
    _, other = await make_club(name="Paying Club", email="paying-club@example.com")
    await _grant(client, header, other.id, shop["growth"].id, complimentary=False, reason=None)

    comps = (await client.get(f"{API}/admin/subscriptions?complimentary=true", headers=header)).json()
    assert [row["club"]["name"] for row in comps["items"]] == ["Billing Club"]
    [row] = comps["items"]
    assert row["comp_reason"] == "Sponsored" and row["comp_ends_at"] is not None
    assert row["granted_by"] == {"id": admin.id, "name": admin.full_name}

    paid = (await client.get(f"{API}/admin/subscriptions?complimentary=false", headers=header)).json()
    assert [row["club"]["name"] for row in paid["items"]] == ["Paying Club"]
    one = (await client.get(f"{API}/admin/subscriptions?club_id={other.id}", headers=header)).json()
    assert [row["club"]["id"] for row in one["items"]] == [other.id]


async def test_a_clubs_plan_can_be_read_before_it_has_a_subscription(client, shop, make_admin, mint_token):
    _, header = await _admin(make_admin, mint_token)
    response = await client.get(f"{API}/admin/subscriptions/club/{shop['club'].id}", headers=header)
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["plan_key"], body["subscription"]) == ("free", None)
    assert body["club"]["id"] == shop["club"].id

    await _grant(client, header, shop["club"].id, shop["pro"].id)
    body = (await client.get(f"{API}/admin/subscriptions/club/{shop['club'].id}", headers=header)).json()
    assert body["plan_key"] == "pro" and body["subscription"]["complimentary"] is True
