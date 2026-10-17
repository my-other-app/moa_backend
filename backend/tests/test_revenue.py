"""The admin's revenue page and the ledger behind it.

- The ledger (subscription_charges): one row per Razorpay payment from
  `subscription.charged`, however often it is delivered; refunds update it;
  a failed attempt is listed and never summed; the backfill (endpoint and
  script) fills it from Razorpay's invoices, read-only.
- The figures: MRR from paying subscriptions only (never a comp, never a
  trial), money from the ledger, months in India -- checked against fixed
  data on either side of a month's first instant in IST.

Razorpay is a fake throughout.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from tests.conftest import auth_header
from tests.test_billing_alerts import _hook, _paid
from tests.test_club_billing import (  # noqa: F401  (razorpay and shop are fixtures)
    FakeRazorpay,
    _row,
    make_plan,
    razorpay,
    shop,
)

API = "/api/v1"
DAY = timedelta(days=1)
IST = timezone(timedelta(hours=5, minutes=30))


def _now():
    return datetime.now(timezone.utc)


def _ts(when: datetime) -> int:
    return int(when.timestamp())


async def _charges(session):
    from app.api.subscriptions.models import SubscriptionCharges

    return list(
        (
            await session.execute(
                select(SubscriptionCharges).order_by(SubscriptionCharges.id).execution_options(populate_existing=True)
            )
        ).scalars()
    )


_seq = {"n": 0}


async def _event(session, name, payload):
    from app.api.payments.service import handle_razorpay_webhook

    _seq["n"] += 1
    await handle_razorpay_webhook(
        session, {"event": name, "entity": "event", "payload": payload},
        event_id=f"evt_rev_{_seq['n']}", signature="sig",
    )


async def _charged(session, sub_id, pay_id, amount=69900, when=None):
    await _hook(session, "subscription.charged", sub_id, paid_count=2,
                current_end=_ts(_now() + 30 * DAY),
                payment={"id": pay_id, "amount": amount, "currency": "INR", "invoice_id": f"inv_{pay_id}",
                         "created_at": _ts(when or _now()), "subscription_id": sub_id})


async def _admin_header(make_admin, mint_token, email="revenue-admin@example.com", role=None):
    return auth_header(await mint_token(await make_admin(email=email, role=role)))


# ─── 1. The ledger ───────────────────────────────────────────────────────


async def test_each_charge_is_recorded_once_however_often_it_arrives(session, shop):
    await _paid(session, shop)
    when = _now() - DAY

    await _charged(session, "sub_paid", "pay_1", when=when)
    await _charged(session, "sub_paid", "pay_1", when=when)  # Razorpay redelivers with a new event id

    [charge] = await _charges(session)
    assert (charge.amount_paise, charge.status, charge.refunded_paise) == (69900, "captured", 0)
    assert (charge.club_id, charge.plan_id, charge.cycle.value) == (shop["club"].id, shop["growth"].id, "monthly")
    assert (charge.razorpay_payment_id, charge.razorpay_invoice_id, charge.razorpay_subscription_id) == (
        "pay_1", "inv_pay_1", "sub_paid")
    assert abs(charge.charged_at - when) < timedelta(seconds=1)

    await _charged(session, "sub_paid", "pay_2")
    assert [c.razorpay_payment_id for c in await _charges(session)] == ["pay_1", "pay_2"]


async def test_refunds_update_the_charge(session, shop, razorpay):
    await _paid(session, shop)
    await _charged(session, "sub_paid", "pay_1")

    # Partial, with the payment's running total on the payload.
    await _event(session, "refund.processed", {
        "refund": {"entity": {"id": "rfnd_1", "payment_id": "pay_1", "amount": 20000}},
        "payment": {"entity": {"id": "pay_1", "amount": 69900, "amount_refunded": 20000}},
    })
    [charge] = await _charges(session)
    assert (charge.refunded_paise, charge.status) == (20000, "partially_refunded")

    # The rest, with no payment on the payload: Razorpay is asked.
    razorpay.handlers[("payment", "fetch")] = lambda pay_id: {"id": pay_id, "amount": 69900, "amount_refunded": 69900}
    await _event(session, "refund.processed", {"refund": {"entity": {"id": "rfnd_2", "payment_id": "pay_1", "amount": 49900}}})
    [charge] = await _charges(session)
    assert (charge.refunded_paise, charge.status) == (69900, "refunded")

    # A ticket refund still goes the ticket way (nothing of ours, no error).
    await _event(session, "refund.processed", {"refund": {"entity": {"id": "rfnd_3", "payment_id": "pay_ticket", "amount": 100}}})
    assert len(await _charges(session)) == 1


async def test_a_failed_charge_is_listed_and_never_counted(client, session, shop, make_admin, mint_token):
    from app.api.subscriptions import revenue

    await _paid(session, shop)
    await _event(session, "payment.failed", {"payment": {"entity": {
        "id": "pay_bad", "subscription_id": "sub_paid", "amount": 69900, "error_description": "Card declined",
        "created_at": _ts(_now()),
    }}})
    [charge] = await _charges(session)
    assert (charge.status, charge.amount_paise) == ("failed", 69900)
    assert (await revenue.collected(session, _now()))["all_time_paise"] == 0

    page = (await client.get(f"{API}/admin/revenue/charges", headers=await _admin_header(make_admin, mint_token))).json()
    assert [row["status"] for row in page["items"]] == ["failed"]
    assert (page["total"], page["sum_paise"]) == (1, 0)


async def test_a_comp_never_creates_a_charge(session, shop):
    from app.api.subscriptions import ledger, service
    from app.api.subscriptions.models import BillingCycle

    row = await service.grant_complimentary(session, club_id=shop["club"].id, plan=shop["pro"],
                                            cycle=BillingCycle.monthly, reason="Partner", ends_at=None, granted_by=None)
    payload = {"payload": {"payment": {"entity": {"id": "pay_x", "amount": 149900}}}}
    assert await ledger.record_charge(session, row, payload) is None
    assert await _charges(session) == []


# ─── 2. Backfill ─────────────────────────────────────────────────────────


def _invoices_fake(razorpay, invoices: dict, payments: dict, broken=()):
    def listing(params):
        if params["subscription_id"] in broken:
            raise RuntimeError("Razorpay is down")
        items = invoices.get(params["subscription_id"], [])
        return {"items": items[params["skip"]: params["skip"] + params["count"]]}

    razorpay.handlers[("invoice", "all")] = listing
    razorpay.handlers[("payment", "fetch")] = lambda pay_id: payments[pay_id]


async def test_the_backfill_adds_what_razorpay_has_and_the_ledger_does_not(
    client, session, shop, make_club, make_admin, mint_token, razorpay
):
    from app.api.subscriptions import service
    from app.api.subscriptions.models import BillingCycle

    await _paid(session, shop)                              # sub_paid, live
    await _charged(session, "sub_paid", "pay_known")        # already in the ledger
    _, comped = await make_club(name="Comped Club", email="comped@example.com")
    await _paid(session, {"club": comped, "growth": shop["growth"]}, sub_id="sub_before_comp")
    await service.grant_complimentary(session, club_id=comped.id, plan=shop["pro"], cycle=BillingCycle.monthly,
                                      reason="Partner", ends_at=None, granted_by=None)
    _, broken = await make_club(name="Broken Club", email="broken@example.com")
    await _paid(session, {"club": broken, "growth": shop["growth"]}, sub_id="sub_broken")
    await session.commit()

    jan = datetime(2026, 1, 5, tzinfo=timezone.utc)
    _invoices_fake(
        razorpay,
        invoices={
            "sub_paid": [
                {"id": "inv_a", "status": "paid", "payment_id": "pay_known", "amount_paid": 69900, "paid_at": _ts(jan)},
                {"id": "inv_b", "status": "paid", "payment_id": "pay_old", "amount_paid": 69900, "paid_at": _ts(jan)},
                {"id": "inv_c", "status": "issued", "payment_id": None, "amount": 69900},
            ],
            # The Razorpay subscription a comp replaced: its payments were revenue.
            "sub_before_comp": [
                {"id": "inv_d", "status": "paid", "payment_id": "pay_pre_comp", "amount_paid": 69900, "paid_at": _ts(jan)},
            ],
        },
        payments={
            "pay_known": {"id": "pay_known", "amount": 69900, "amount_refunded": 10000, "created_at": _ts(jan)},
            "pay_old": {"id": "pay_old", "amount": 69900, "amount_refunded": 0, "created_at": _ts(jan)},
            "pay_pre_comp": {"id": "pay_pre_comp", "amount": 69900, "amount_refunded": 69900, "created_at": _ts(jan)},
        },
        broken=("sub_broken",),
    )
    header = await _admin_header(make_admin, mint_token)

    first = await client.post(f"{API}/admin/revenue/backfill", headers=header)
    assert first.status_code == 200, first.text
    assert first.json() == {"scanned": 3, "inserted": 2, "updated": 1, "failed": 1}
    by_id = {c.razorpay_payment_id: c for c in await _charges(session)}
    assert set(by_id) == {"pay_known", "pay_old", "pay_pre_comp"}
    assert (by_id["pay_known"].refunded_paise, by_id["pay_known"].status) == (10000, "partially_refunded")
    assert (by_id["pay_pre_comp"].club_id, by_id["pay_pre_comp"].plan_id) == (comped.id, shop["growth"].id)
    assert by_id["pay_pre_comp"].status == "refunded"
    assert by_id["pay_old"].charged_at == jan and by_id["pay_old"].razorpay_invoice_id == "inv_b"
    # Read-only towards Razorpay.
    assert {(r, m) for r, m, _ in razorpay.calls} <= {("invoice", "all"), ("payment", "fetch")}

    again = await client.post(f"{API}/admin/revenue/backfill", headers=header)
    assert again.json() == {"scanned": 3, "inserted": 0, "updated": 0, "failed": 1}


async def test_the_backfill_script_does_the_same(session, shop, razorpay):
    from scripts.backfill_subscription_charges import run

    await _paid(session, shop)
    _invoices_fake(
        razorpay,
        invoices={"sub_paid": [{"id": "inv_1", "status": "paid", "payment_id": "pay_s", "amount_paid": 69900,
                                "paid_at": _ts(_now())}]},
        payments={"pay_s": {"id": "pay_s", "amount": 69900, "created_at": _ts(_now())}},
    )
    assert await run(session, client=razorpay) == {"scanned": 1, "inserted": 1, "updated": 0, "failed": 0}
    assert [c.razorpay_payment_id for c in await _charges(session)] == ["pay_s"]


async def test_the_backfill_needs_the_money_permission(client, make_admin, mint_token):
    from app.api.users.models import AdminRoles

    header = await _admin_header(make_admin, mint_token, email="viewer-rev@example.com", role=AdminRoles.viewer)
    assert (await client.post(f"{API}/admin/revenue/backfill", headers=header)).status_code == 403


# ─── 3. The figures ──────────────────────────────────────────────────────


#: A fixed "now": 15 March 2026, midday in India.
NOW = datetime(2026, 3, 15, 6, 30, tzinfo=timezone.utc)


async def _world(session, make_club):
    """Clubs in every state the page counts, and payments either side of
    month boundaries in India."""
    from app.api.admin_alerts.models import AdminNotifications
    from app.api.audit.models import AdminAuditLogs
    from app.api.subscriptions.models import ClubSubscriptions, SubscriptionCharges

    free = await make_plan(session, key="free")
    growth = await make_plan(session, key="growth", monthly=69900, yearly=699000)
    pro = await make_plan(session, key="pro", monthly=149900, yearly=1499000)

    async def club(name, verified=True):
        _, made = await make_club(name=name, email=f"{name.lower().replace(' ', '')}@example.com", is_verified=verified)
        return made

    clubs = {}
    rows = [
        # name, plan, extra
        ("Monthly Payer", growth, dict(status="active", cycle="monthly", mrr_paise=69900, razorpay_subscription_id="sub_m",
                                       activated_at=NOW - 40 * DAY, current_period_end=NOW + 10 * DAY)),
        ("Yearly Payer", pro, dict(status="active", cycle="yearly", mrr_paise=1499000 // 12, razorpay_subscription_id="sub_y",
                                   activated_at=NOW - 40 * DAY, current_period_end=NOW + 200 * DAY)),
        ("Leaving Payer", growth, dict(status="active", cycle="monthly", mrr_paise=69900, razorpay_subscription_id="sub_l",
                                       activated_at=NOW - 40 * DAY, current_period_end=NOW + 5 * DAY,
                                       cancel_at_period_end=True)),
        ("Comped", pro, dict(status="active", cycle="monthly", mrr_paise=0, complimentary=True,
                             comp_reason="Partner", comp_ends_at=NOW + 30 * DAY, current_period_end=NOW + 30 * DAY)),
        ("Comp Over", pro, dict(status="active", cycle="monthly", mrr_paise=0, complimentary=True,
                                comp_reason="Expired", comp_ends_at=NOW - DAY)),
        ("Trialing", growth, dict(status="trial", cycle="monthly", razorpay_subscription_id="sub_t",
                                  authenticated_at=NOW - 2 * DAY, trial_started_at=NOW - 2 * DAY,
                                  trial_ends_at=NOW + 12 * DAY)),
        ("In Grace", growth, dict(status="past_due", cycle="monthly", mrr_paise=69900, razorpay_subscription_id="sub_g",
                                  activated_at=NOW - 60 * DAY, halted_at=NOW - DAY, grace_ends_at=NOW + 6 * DAY)),
        ("Grace Over", growth, dict(status="past_due", cycle="monthly", mrr_paise=69900, razorpay_subscription_id="sub_go",
                                    activated_at=NOW - 60 * DAY, halted_at=NOW - 9 * DAY, grace_ends_at=NOW - 2 * DAY)),
        ("Paused", growth, dict(status="paused", cycle="monthly", razorpay_subscription_id="sub_p")),
        ("Pending", growth, dict(status="pending", cycle="monthly", razorpay_subscription_id="sub_pe", pending_since=NOW)),
        ("Free By Hand", free, dict(status="active", cycle="monthly", mrr_paise=0)),
    ]
    for name, plan, extra in rows:
        clubs[name] = await club(name)
        session.add(ClubSubscriptions(club_id=clubs[name].id, plan_id=plan.id, **extra))
    clubs["Nothing"] = await club("Nothing")
    clubs["Unapproved"] = await club("Unapproved", verified=False)
    gone = await club("Gone Payer")
    session.add(ClubSubscriptions(club_id=gone.id, plan_id=growth.id, status="active", mrr_paise=69900,
                                  activated_at=NOW - 40 * DAY))
    gone.soft_delete()
    await session.flush()

    def charge(pay_id, club_name, at, amount=69900, refunded=0, status="captured", sub="sub_m"):
        session.add(SubscriptionCharges(
            club_id=clubs[club_name].id if club_name != "Gone Payer" else gone.id, plan_id=growth.id, cycle="monthly",
            amount_paise=amount, razorpay_payment_id=pay_id, razorpay_subscription_id=sub, status=status,
            refunded_paise=refunded, charged_at=at,
        ))

    # 23:59:59 on 28 Feb in India: February. One second later: March.
    charge("p_feb_last", "Monthly Payer", datetime(2026, 2, 28, 18, 29, 59, tzinfo=timezone.utc))
    charge("p_mar_first", "Monthly Payer", datetime(2026, 2, 28, 18, 30, 0, tzinfo=timezone.utc), refunded=10000,
           status="partially_refunded")
    # 00:30 on 1 Feb in India, though still 31 January in UTC: February.
    charge("p_feb_first", "Leaving Payer", datetime(2026, 1, 31, 19, 0, tzinfo=timezone.utc), sub="sub_l")
    # 1 January 2026 in India (still 2025 in UTC): this year.
    charge("p_jan", "In Grace", datetime(2025, 12, 31, 18, 30, tzinfo=timezone.utc), sub="sub_g")
    # 31 December 2025 in India: last year.
    charge("p_dec", "In Grace", datetime(2025, 12, 31, 18, 29, tzinfo=timezone.utc), sub="sub_g")
    charge("p_yearly", "Yearly Payer", datetime(2025, 10, 3, tzinfo=timezone.utc), amount=1499000, sub="sub_y")
    charge("p_failed", "Monthly Payer", datetime(2026, 3, 2, tzinfo=timezone.utc), status="failed", sub="sub_m")
    charge("p_gone", "Gone Payer", datetime(2026, 3, 3, tzinfo=timezone.utc), sub="sub_gone")

    # Cancellations as alerted; a comp revoked is not one.
    for at, data in (
        (datetime(2026, 3, 1, 0, 0, tzinfo=timezone.utc), {"by": "club"}),
        (datetime(2026, 2, 28, 18, 0, tzinfo=timezone.utc), {"by": "admin"}),        # 23:30 IST: February
        (datetime(2026, 3, 4, tzinfo=timezone.utc), {"by": "admin", "complimentary": True}),
    ):
        session.add(AdminNotifications(kind="subscription_cancelled", title="x", club_id=clubs["Leaving Payer"].id,
                                       data=data, created_at=at))
    session.add(AdminNotifications(kind="subscription_downgraded", title="x", data={},
                                   created_at=datetime(2026, 1, 20, tzinfo=timezone.utc)))
    session.add(AdminAuditLogs(action="subscription.comp_grant", entity_type="club", summary="x",
                               created_at=datetime(2026, 2, 28, 18, 30, tzinfo=timezone.utc)))   # 1 March in India
    await session.commit()
    return dict(clubs=clubs, free=free, growth=growth, pro=pro)


async def test_every_figure_from_fixed_data(session, make_club):
    from app.api.subscriptions import revenue

    world = await _world(session, make_club)
    out = await revenue.revenue(session, months=4, now=NOW)

    yearly_mrr = 1499000 // 12
    # Paying: the two monthly payers, the yearly one, and the one within its
    # grace. Never the comp, the trial, the lapsed grace or the deleted club.
    assert out["mrr_paise"] == 69900 * 3 + yearly_mrr
    assert out["arr_paise"] == out["mrr_paise"] * 12
    assert out["counts"] == {
        "paying": 4, "trialing": 1, "complimentary": 1, "past_due": 2, "in_grace": 1, "paused": 1, "pending": 1,
        # 12 approved, live clubs (not Unapproved, not Gone Payer); on a plan:
        # the three payers, In Grace, Comped and Trialing.
        "free_clubs": 12 - 6, "total_clubs": 12,
    }
    plans = {row["plan_key"]: row for row in out["by_plan"]}
    assert plans["growth"] == {"plan_id": world["growth"].id, "plan_key": "growth", "plan_name": "Growth",
                               "paying": 3, "trialing": 1, "complimentary": 0, "mrr_paise": 69900 * 3}
    assert plans["pro"] == {"plan_id": world["pro"].id, "plan_key": "pro", "plan_name": "Pro",
                            "paying": 1, "trialing": 0, "complimentary": 1, "mrr_paise": yearly_mrr}
    assert plans["free"]["paying"] == 0 and plans["free"]["mrr_paise"] == 0
    assert out["by_cycle"] == {"monthly": {"paying": 3, "mrr_paise": 69900 * 3},
                               "yearly": {"paying": 1, "mrr_paise": yearly_mrr}}

    # Gross; the failed attempt never; the deleted club's payment still money.
    assert out["collected"] == {
        "this_month_paise": 69900 + 69900,                 # p_mar_first, p_gone
        "last_month_paise": 69900 + 69900,                 # p_feb_last, p_feb_first
        "year_to_date_paise": 69900 * 5,                   # + p_jan
        "all_time_paise": 69900 * 6 + 1499000,             # + p_dec, p_yearly
        "refunded_paise": 10000,
    }
    assert out["series"] == [
        {"month": "2025-12", "collected_paise": 69900, "refunded_paise": 0, "new_paying": 1, "new_trials": 0,
         "cancelled": 0, "new_complimentary": 0},
        {"month": "2026-01", "collected_paise": 69900, "refunded_paise": 0, "new_paying": 0, "new_trials": 0,
         "cancelled": 1, "new_complimentary": 0},
        {"month": "2026-02", "collected_paise": 69900 * 2, "refunded_paise": 0, "new_paying": 2, "new_trials": 0,
         "cancelled": 1, "new_complimentary": 0},
        {"month": "2026-03", "collected_paise": 69900 * 2, "refunded_paise": 10000, "new_paying": 1, "new_trials": 1,
         "cancelled": 1, "new_complimentary": 1},
    ]
    # Renewing within 30 days: the monthly payer, at what it last paid. Not
    # the one cancelling at period end, not the yearly one.
    assert out["upcoming_renewals"] == {"next_30_days_paise": 69900, "count": 1}


async def test_new_paying_is_a_subscriptions_first_payment(session, make_club):
    from app.api.subscriptions import revenue

    await _world(session, make_club)
    series = {row["month"]: row for row in await revenue.series(session, 12, NOW)}
    # sub_m first paid on 28 Feb in India; sub_l on 1 Feb; sub_g on 31 Dec;
    # sub_y in October; sub_gone in March.
    assert series["2025-10"]["new_paying"] == 1
    assert series["2025-12"]["new_paying"] == 1
    assert series["2026-02"]["new_paying"] == 2
    assert series["2026-03"]["new_paying"] == 1
    assert len(series) == 12 and min(series) == "2025-04"


async def test_ticket_money_is_shown_apart_and_only_what_was_kept(session, make_club):
    from app.api.payments.models import OrderStatus, PaymentLogs, PaymentOrders, PaymentStatus
    from app.api.subscriptions import revenue

    for i, (status, amount, at) in enumerate((
        (PaymentStatus.captured, Decimal("499.00"), datetime(2026, 3, 2, tzinfo=timezone.utc)),
        (PaymentStatus.captured, Decimal("250.50"), datetime(2026, 2, 28, 18, 29, tzinfo=timezone.utc)),
        (PaymentStatus.refunded, Decimal("999.00"), datetime(2026, 3, 3, tzinfo=timezone.utc)),
        (PaymentStatus.failed, Decimal("100.00"), datetime(2026, 3, 3, tzinfo=timezone.utc)),
    )):
        order = PaymentOrders(receipt=f"r{i}", razorpay_order_id=f"order_{i}", amount=amount, currency="INR",
                              status=OrderStatus.paid, source="event_registration", payload={})
        session.add(order)
        await session.flush()
        session.add(PaymentLogs(order_id=order.id, razorpay_payment_id=f"pay_t{i}", status=status, amount=amount,
                                created_at=at))
    await session.commit()

    sales = await revenue.ticket_sales(session, NOW)
    assert (sales["this_month_paise"], sales["all_time_paise"]) == (49900, 49900 + 25050)
    assert "not platform revenue" in sales["note"]
    assert (await revenue.revenue(session, now=NOW))["collected"]["all_time_paise"] == 0


async def test_the_revenue_page_over_http(client, session, shop, make_admin, mint_token):
    from app.api.users.models import AdminRoles

    await _paid(session, shop)
    await _charged(session, "sub_paid", "pay_http")
    # Any admin may look, including one who cannot move money.
    header = await _admin_header(make_admin, mint_token, email="viewer-page@example.com", role=AdminRoles.viewer)

    for months in (6, 12, 24):
        response = await client.get(f"{API}/admin/revenue?months={months}", headers=header)
        assert response.status_code == 200, response.text
        body = response.json()
        assert len(body["series"]) == months
        assert body["series"][-1]["month"] == _now().astimezone(IST).strftime("%Y-%m")
    assert body["mrr_paise"] == 69900 and body["counts"]["paying"] == 1
    assert body["collected"]["this_month_paise"] == 69900
    assert set(body) == {"mrr_paise", "arr_paise", "counts", "by_plan", "by_cycle", "collected", "series",
                         "upcoming_renewals", "ticket_sales"}
    assert (await client.get(f"{API}/admin/revenue?months=0", headers=header)).status_code == 422
    assert (await client.get(f"{API}/admin/revenue", headers=shop["h"])).status_code in (401, 403)


# ─── 4. The payments list ────────────────────────────────────────────────


@pytest.mark.parametrize("month,expected", [("2026-02", ["p_feb_last", "p_feb_first"]),
                                            ("2026-03", ["p_failed", "p_gone", "p_mar_first"])])
async def test_the_charges_list_by_month_in_india(client, session, make_club, make_admin, mint_token, month, expected):
    await _world(session, make_club)
    header = await _admin_header(make_admin, mint_token)
    page = (await client.get(f"{API}/admin/revenue/charges?month={month}&limit=50", headers=header)).json()
    assert sorted(row["razorpay_payment_id"] for row in page["items"]) == sorted(expected)
    assert page["total"] == len(expected)


async def test_the_charges_list_filters_pages_and_sums(client, session, make_club, make_admin, mint_token):
    world = await _world(session, make_club)
    header = await _admin_header(make_admin, mint_token)
    payer = world["clubs"]["Monthly Payer"]

    first = (await client.get(f"{API}/admin/revenue/charges?club_id={payer.id}&limit=2", headers=header)).json()
    assert first["total"] == 3
    # Every matching payment summed, not just this page, and never the failed one.
    assert (first["sum_paise"], first["refunded_paise"]) == (69900 * 2, 10000)
    assert [row["razorpay_payment_id"] for row in first["items"]] == ["p_failed", "p_mar_first"]
    row = first["items"][1]
    assert row["club"] == {"id": payer.id, "name": "Monthly Payer", "slug": payer.slug}
    assert (row["plan_name"], row["cycle"], row["amount_paise"], row["refunded_paise"], row["status"]) == (
        "Growth", "monthly", 69900, 10000, "partially_refunded")
    second = (await client.get(f"{API}/admin/revenue/charges?club_id={payer.id}&limit=2&offset=2", headers=header)).json()
    assert [row["razorpay_payment_id"] for row in second["items"]] == ["p_feb_last"]

    both = (await client.get(f"{API}/admin/revenue/charges?club_id={payer.id}&month=2026-03", headers=header)).json()
    assert both["total"] == 2
    # A deleted club's payment is still listed, with its club.
    everything = (await client.get(f"{API}/admin/revenue/charges?limit=100", headers=header)).json()
    gone = [row for row in everything["items"] if row["razorpay_payment_id"] == "p_gone"]
    assert gone[0]["club"]["name"] == "Gone Payer"
    assert (await client.get(f"{API}/admin/revenue/charges?month=2026-13", headers=header)).status_code == 422
    assert (await client.get(f"{API}/admin/revenue/charges?month=March", headers=header)).status_code == 422
