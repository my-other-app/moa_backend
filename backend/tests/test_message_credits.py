"""Prepaid message credits, and the overage billing they replace.

What is checked: a send uses the plan's allowance first, then the credit
lot that expires first; expired, used and refunded lots are never drawn
on; tickets, certificates and payment messages always go (counted, never
charged) while the rest is held back with nothing left, on every plan;
bought credits are not held to the promotional cap; buying is a one-time
Razorpay order applied once, by checkout's verify, the webhook or the sweep,
whichever is first; refunds take the unused credits back and are never made
twice; grants are audited; the club is told when credits run low and a
month before they expire; nothing is charged on 1 November, whatever the old
flag says, and open statements are void; the club mailbox's contract.

Razorpay is LIVE in production: every call here goes to a fake.
"""
import hashlib
import hmac
import itertools
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from razorpay.errors import BadRequestError
from sqlalchemy import func, select

from app.api.audit.models import AdminAuditLogs
from app.api.club_alerts.models import ClubAlerts
from app.api.message_quota import billing, credits, service as quota
from app.api.message_quota.models import (
    MessageCreditLots,
    MessageCreditPacks,
    MessageCreditPurchases,
    MessageStatements,
    MessageUsage,
)
from app.api.payments import service as payments
from app.config import settings
from tests.conftest import auth_header
from tests.test_billing_alerts import mail  # noqa: F401  (a fixture)
from tests.test_club_billing import FakeRazorpay as FakeSubscriptions, make_plan

API = "/api/v1"
DAY = timedelta(days=1)
IST = timezone(timedelta(hours=5, minutes=30))
LONG_AGO = datetime(2020, 1, 1, tzinfo=timezone.utc)
_n = itertools.count(1)


def _now():
    return datetime.now(timezone.utc)


# ─── Fakes and the world ─────────────────────────────────────────────────


class FakeRazorpay:
    """The platform's account: orders, payments in them, refunds."""

    def __init__(self):
        self.orders: dict[str, dict] = {}
        self.payments: dict[str, dict] = {}
        self.calls: list[tuple] = []
        self.refund_error = None
        self.order = SimpleNamespace(create=self._order_create, payments=self._order_payments)
        self.payment = SimpleNamespace(fetch=self._payment_fetch, refund=self._payment_refund)

    def _order_create(self, data=None, **kwargs):
        order_id = f"order_cr_{next(_n)}"
        self.calls.append(("order.create", order_id, data["amount"]))
        self.orders[order_id] = dict(data)
        return {"id": order_id, "amount": data["amount"], "status": "created"}

    def _order_payments(self, order_id, *args, **kwargs):
        self.calls.append(("order.payments", order_id))
        return {"items": [dict(p) for p in self.payments.values() if p["order_id"] == order_id]}

    def _payment_fetch(self, payment_id, *args, **kwargs):
        self.calls.append(("payment.fetch", payment_id))
        return dict(self.payments[payment_id])

    def _payment_refund(self, payment_id, data=None, *args, **kwargs):
        self.calls.append(("payment.refund", payment_id, (data or {}).get("amount")))
        error = self.refund_error
        if error == "made-then-timeout":
            self.payments[payment_id]["amount_refunded"] += data["amount"]
            from app.api.payments.rzp import RazorpayTimeout

            raise RazorpayTimeout("Razorpay did not answer refund within 10 s")
        if error is not None:
            raise error
        self.payments[payment_id]["amount_refunded"] += data["amount"]
        return {"id": f"rfnd_{next(_n)}", "payment_id": payment_id, "amount": data["amount"]}

    def pay(self, order_id, *, status="captured", method="upi", amount=None) -> dict:
        payment_id = f"pay_cr_{next(_n)}"
        self.payments[payment_id] = {
            "id": payment_id, "order_id": order_id, "status": status, "method": method,
            "amount": self.orders[order_id]["amount"] if amount is None else amount, "amount_refunded": 0,
        }
        return self.payments[payment_id]

    def of(self, name):
        return [c for c in self.calls if c[0] == name]


@pytest.fixture
def rzp(monkeypatch):
    fake = FakeRazorpay()
    monkeypatch.setattr(payments, "razorpay_client", fake)
    return fake


@pytest.fixture(autouse=True)
def _enforced(monkeypatch):
    from app.api.subscriptions import gates

    monkeypatch.setattr(settings, "PLAN_ENFORCEMENT", True)
    monkeypatch.setattr(settings, "PLAN_ENFORCEMENT_FROM", LONG_AGO)
    gates.reset()
    yield
    gates.reset()


@pytest.fixture
async def world(session, make_club, mint_token):
    """Free (3 emails, no WhatsApp) and Growth (3 emails, 2 WhatsApp, of
    which 1 promotional; Razorpay billed), Enterprise (unlimited), and a
    club on Free."""
    free = await make_plan(session, key="free", limits={"emails_per_month": 3, "whatsapp_per_month": 0,
                                                        "whatsapp_marketing_per_month": 0})
    growth = await make_plan(session, key="growth", monthly=69900, yearly=699000,
                             limits={"emails_per_month": 3, "whatsapp_per_month": 2, "whatsapp_marketing_per_month": 1},
                             features=("tickets",))
    enterprise = await make_plan(session, key="enterprise", contact_sales=True, features=("tickets",))
    owner, club = await make_club(name="Credit Club", email="credit-owner@example.com")
    await session.commit()
    return SimpleNamespace(owner=owner, club=club, free=free, growth=growth, enterprise=enterprise,
                           h=auth_header(await mint_token(owner)))


async def _paying(session, club_id, plan_id, sub_id="sub_credits_live"):
    from app.api.subscriptions.models import ClubSubscriptions

    now = _now()
    session.add(ClubSubscriptions(club_id=club_id, plan_id=plan_id, status="active", razorpay_subscription_id=sub_id,
                                  authenticated_at=now - 40 * DAY, activated_at=now - 40 * DAY,
                                  current_period_end=now + 20 * DAY, mrr_paise=69900, price_paise=69900))
    await session.commit()


async def _lot(session, club_id, channel="email", quantity=10, *, remaining=None, expires_in=300, bought_ago=1,
               source="grant") -> MessageCreditLots:
    now = _now()
    lot = MessageCreditLots(club_id=club_id, channel=channel, source=source, quantity=quantity,
                            remaining=quantity if remaining is None else remaining, bought_at=now - bought_ago * DAY,
                            expires_at=now + expires_in * DAY, reason="test" if source == "grant" else None)
    session.add(lot)
    await session.commit()
    return lot


async def _fresh(session, model, row_id):
    return await session.scalar(select(model).where(model.id == row_id).execution_options(populate_existing=True))


async def _usage(session, club_id, channel="email") -> MessageUsage:
    return await session.scalar(
        select(MessageUsage).where(MessageUsage.club_id == club_id, MessageUsage.channel == channel,
                                   MessageUsage.period == quota.period_of()).execution_options(populate_existing=True)
    )


async def _send(session, club_id, kind="announcement", channel="email", n=1, **kw):
    """`n` sends as a sender makes them: checked, then counted when they go.
    Returns how many were held back."""
    held = 0
    for _ in range(n):
        terms, reason = await quota.check(session, club_id, channel, kind, **kw)
        if reason:
            held += 1
        else:
            await quota.record_message(club_id, channel, kind, session=session, terms=terms, **kw)
        await session.commit()
    return held


async def _seed_packs(session):
    from tests.test_trial_pricing import _migration

    migration = _migration("message_credits")
    connection = await session.connection()
    made = await connection.run_sync(lambda sync: migration.seed_packs(sync))
    await session.commit()
    return made


# ─── Using credits ───────────────────────────────────────────────────────


async def test_sends_use_the_plan_first_then_the_lot_that_expires_first(session, world):
    club_id = world.club.id
    later = await _lot(session, club_id, quantity=5, expires_in=200)
    sooner = await _lot(session, club_id, quantity=2, expires_in=100)
    expired = await _lot(session, club_id, quantity=50, expires_in=-1, bought_ago=400)
    whatsapp = await _lot(session, club_id, channel="whatsapp", quantity=9)

    assert await _send(session, club_id, n=3) == 0  # Free's 3 emails
    assert ((await _fresh(session, MessageCreditLots, sooner.id)).remaining,
            (await _fresh(session, MessageCreditLots, later.id)).remaining) == (2, 5)
    assert await _send(session, club_id, n=4) == 0  # 2 from the sooner lot, then 2 from the later
    assert (await _fresh(session, MessageCreditLots, sooner.id)).remaining == 0
    assert (await _fresh(session, MessageCreditLots, later.id)).remaining == 3
    # An expired lot, and another channel's, are never drawn on.
    assert (await _fresh(session, MessageCreditLots, expired.id)).remaining == 50
    assert (await _fresh(session, MessageCreditLots, whatsapp.id)).remaining == 9
    row = await _usage(session, club_id)
    assert (row.used, row.plan_used, row.credits_used, row.uncovered, row.blocked) == (7, 3, 4, 0, 0)

    usage = await quota.usage(session, club_id)
    email = usage["channels"]["email"]
    assert (email["state"], email["plan_remaining"], email["credits_remaining"], email["total_remaining"]) == ("credits", 0, 3, 3)
    assert await credits.remaining(session, club_id, "email") == 3


async def test_with_nothing_left_the_rest_is_held_back_but_essentials_always_go(session, world):
    club_id = world.club.id
    await _lot(session, club_id, quantity=1)
    assert await _send(session, club_id, n=4) == 0  # 3 from Free, 1 credit
    # Nothing left: an announcement, a reminder, a mailbox mail wait ...
    assert await _send(session, club_id, kind="announcement") == 1
    terms = await quota.terms_for(session, club_id)
    reason = await quota.held_back(session, terms, "email", "reminder")
    assert reason == ("Not sent: this month's 3 emails on Free are used up and there are no email credits left. "
                      "Buy credits to send more; tickets and certificates still go.")
    await session.commit()
    # ... tickets, certificates and payment messages always go, counted, never charged.
    for kind in ("ticket", "certificate", "payment"):
        assert await _send(session, club_id, kind=kind) == 0
    row = await _usage(session, club_id)
    assert (row.used, row.plan_used, row.credits_used, row.uncovered, row.blocked) == (7, 3, 1, 3, 2)
    assert (await quota.usage(session, club_id))["channels"]["email"]["state"] == "blocked"


async def test_a_paying_plan_is_held_back_too_and_credits_let_it_go_on(session, world):
    """No plan sends past its allowance on credit any more: Growth, billed
    by Razorpay, holds back like Free until it has credits."""
    club_id = world.club.id
    await _paying(session, club_id, world.growth.id)
    assert await _send(session, club_id, n=4) == 1
    await _lot(session, club_id, quantity=2)
    assert await _send(session, club_id, n=3) == 1
    row = await _usage(session, club_id)
    assert (row.used, row.plan_used, row.credits_used, row.blocked) == (5, 3, 2, 2)


async def test_credits_send_what_free_does_not_include(session, world):
    """Free has no WhatsApp: bought WhatsApp credits send its reminders."""
    club_id = world.club.id
    terms = await quota.terms_for(session, club_id)
    assert await quota.held_back(session, terms, "whatsapp", "reminder") is not None
    await session.commit()
    await _lot(session, club_id, channel="whatsapp", quantity=1)
    assert await _send(session, club_id, kind="reminder", channel="whatsapp") == 0
    assert (await _usage(session, club_id, "whatsapp")).credits_used == 1
    assert await _send(session, club_id, kind="reminder", channel="whatsapp") == 1


async def test_bought_credits_are_not_held_to_the_promotional_cap(session, world):
    club_id = world.club.id
    await _paying(session, club_id, world.growth.id)
    promo = {"kind": "promotion", "channel": "whatsapp"}
    assert await _send(session, club_id, **promo) == 0  # the plan's 1 promotional
    assert await _send(session, club_id, **promo) == 1  # its cap: held back, no credits
    await _lot(session, club_id, channel="whatsapp", quantity=2)
    assert await _send(session, club_id, n=2, **promo) == 0  # from credits, past the cap
    assert await _send(session, club_id, **promo) == 1
    row = await _usage(session, club_id, "whatsapp")
    assert (row.used, row.marketing, row.plan_used, row.credits_used, row.blocked) == (3, 3, 1, 2, 2)
    # A utility message still has the plan's second WhatsApp.
    assert await _send(session, club_id, kind="update", channel="whatsapp") == 0
    assert (await _usage(session, club_id, "whatsapp")).plan_used == 2


async def test_an_unlimited_plan_never_touches_its_credits(session, world):
    from app.api.subscriptions import service as subs
    from app.api.subscriptions.models import BillingCycle

    club_id = world.club.id
    await subs.assign_plan(session, club_id=club_id, plan=world.enterprise, cycle=BillingCycle.yearly)
    await session.commit()
    lot = await _lot(session, club_id, quantity=5)
    assert await _send(session, club_id, n=10) == 0
    assert (await _fresh(session, MessageCreditLots, lot.id)).remaining == 5
    assert await credits.remaining(session, club_id, "email") == credits.UNLIMITED
    # The club mailbox shows that as no limit, not as a number.
    from app.api.club_mailbox import credits as mailbox_credits

    assert await mailbox_credits.remaining(session, club_id) is None


# ─── The club mailbox's contract ─────────────────────────────────────────


async def test_the_mailbox_contract(session, world):
    """The club mailbox (yourclub@mail.myotherapp.in) checks before each
    mail and counts it once sent: `held_back`/`check` and `record_message`
    keep their names and signatures, "mailbox" is a non-essential kind, and
    `credits.remaining` says what is left."""
    club_id = world.club.id
    from app.api.club_mailbox import credits as mailbox_credits

    assert "mailbox" in quota.KINDS and "mailbox" not in quota.TRANSACTIONAL
    assert await credits.remaining(session, club_id, "email") == 3
    await _lot(session, club_id, quantity=2)
    assert await credits.remaining(session, club_id, "email") == 5
    # The mailbox's own reading of it (app/api/club_mailbox/credits.py).
    assert await mailbox_credits.remaining(session, club_id) == 5

    terms = await quota.terms_for(session, club_id)
    for i in range(5):
        assert await quota.held_back(session, terms, "email", "mailbox") is None
        done = await quota.record_message(club_id, "email", "mailbox", via="platform", session=session, ref=f"mailbox:{i}")
        assert done.counted
        await session.commit()
    assert done.from_credits == 1 and done.from_plan == 0
    assert await credits.remaining(session, club_id, "email") == 0
    assert (await quota.held_back(session, terms, "email", "mailbox")).startswith("Not sent: this month's 3 emails")
    _, reason = await quota.check(session, club_id, "email", "mailbox")
    assert reason is not None
    row = await _usage(session, club_id)
    assert row.kinds == {"mailbox": 5} and row.blocked == 2
    # Recorded twice (a retried job): counted once.
    again = await quota.record_message(club_id, "email", "mailbox", via="platform", session=session, ref="mailbox:4")
    assert again.counted is False
    with pytest.raises(ValueError):
        await credits.remaining(session, club_id, "sms")


# ─── Buying ──────────────────────────────────────────────────────────────


def _signature(order_id, payment_id):
    return hmac.new(settings.RAZORPAY_KEY_SECRET.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()


async def _buy(client, world, pack_id):
    r = await client.post(f"{API}/clubs/messages/credits/orders", json={"pack_id": pack_id}, headers=world.h)
    assert r.status_code == 200, r.text
    return r.json()


async def _pack(session, channel="email", quantity=1000) -> MessageCreditPacks:
    return await session.scalar(
        select(MessageCreditPacks).where(MessageCreditPacks.channel == channel, MessageCreditPacks.quantity == quantity)
    )


async def test_the_default_packs_are_on_the_pricing_page(client, session):
    assert await _seed_packs(session) == 6
    assert await _seed_packs(session) == 0  # once
    r = await client.get(f"{API}/credit-packs")
    assert r.status_code == 200 and "max-age=300" in r.headers["cache-control"]
    body = r.json()
    assert (body["currency"], body["prices_include_gst"], body["validity_months"]) == ("INR", True, 12)
    assert [(p["channel"], p["quantity"], p["price_paise"]) for p in body["packs"]] == [
        ("email", 1000, 9900), ("email", 5000, 44900), ("email", 20000, 159900),
        ("whatsapp", 100, 15000), ("whatsapp", 500, 69900), ("whatsapp", 2000, 259900),
    ]


async def test_a_pack_is_bought_through_checkout_and_its_credits_come_once(client, session, world, rzp, mail):  # noqa: F811
    await _seed_packs(session)
    pack = await _pack(session, "email", 5000)
    order = await _buy(client, world, pack.id)
    assert (order["amount_paise"], order["currency"], order["key_id"]) == (44900, "INR", settings.RAZORPAY_KEY_ID)
    assert order["description"] == "5,000 email credits" and order["receipt_number"].startswith("MOA-CR-")
    assert rzp.orders[order["razorpay_order_id"]]["notes"]["kind"] == "message_credits"
    # Nothing yet: an order is not a payment.
    assert await credits.bought_remaining(session, world.club.id, "email") == 0

    payment = rzp.pay(order["razorpay_order_id"])
    verify = {"razorpay_order_id": order["razorpay_order_id"], "razorpay_payment_id": payment["id"],
              "razorpay_signature": _signature(order["razorpay_order_id"], payment["id"])}
    bad = await client.post(f"{API}/clubs/messages/credits/verify", json={**verify, "razorpay_signature": "nope"}, headers=world.h)
    assert bad.status_code == 400 and bad.json()["error_code"] == "BAD_SIGNATURE"
    r = await client.post(f"{API}/clubs/messages/credits/verify", json=verify, headers=world.h)
    assert r.status_code == 200, r.text
    bought = r.json()
    assert (bought["status"], bought["credits_remaining"], bought["payment_method"]) == ("paid", 5000, "upi")
    # Verified again, and the webhook arriving after: still one lot.
    assert (await client.post(f"{API}/clubs/messages/credits/verify", json=verify, headers=world.h)).json()["status"] == "paid"
    event = {"entity": "event", "event": "payment.captured", "payload": {"payment": {"entity": dict(payment)}}}
    await payments.handle_razorpay_webhook(session, event, event_id="evt_cr_1", signature="sig")
    await payments.handle_razorpay_webhook(session, event, event_id="evt_cr_1", signature="sig")
    assert await session.scalar(select(func.count(MessageCreditLots.id))) == 1
    lot = await session.scalar(select(MessageCreditLots))
    assert (lot.source, lot.quantity, lot.remaining) == ("purchase", 5000, 5000)
    assert credits.add_months(lot.bought_at, 12) == lot.expires_at

    view = (await client.get(f"{API}/clubs/messages/credits", headers=world.h)).json()
    assert view["can_buy"] is True and view["prices_include_gst"] is True and len(view["packs"]) == 6
    assert view["channels"]["email"]["bought"]["remaining"] == 5000
    assert view["channels"]["email"]["total_remaining"] == 5003
    assert [p["status"] for p in view["purchases"]] == ["paid"]
    receipt = (await client.get(f"{API}/clubs/messages/credits/purchases/{bought['id']}/receipt", headers=world.h)).json()
    assert (receipt["item"], receipt["amount_paise"], receipt["prices_include_gst"]) == ("5,000 email credits, valid 12 months", 44900, True)
    assert receipt["club"]["name"] == "Credit Club" and receipt["razorpay_payment_id"] == payment["id"]
    audit = await session.scalar(select(AdminAuditLogs).where(AdminAuditLogs.action == "messages.credits_bought"))
    assert audit is not None and audit.details["amount_paise"] == 44900
    await mail.drain()
    [sent] = [m for m in mail.to("credit-owner@example.com") if m["subject"].startswith("Receipt MOA-CR-")]
    assert "GST included" in sent["text"]


async def test_the_webhook_alone_brings_the_credits_and_an_authorised_payment_waits(client, session, world, rzp):
    await _seed_packs(session)
    order = await _buy(client, world, (await _pack(session, "whatsapp", 100)).id)
    authorised = rzp.pay(order["razorpay_order_id"], status="authorized")
    r = await client.post(f"{API}/clubs/messages/credits/verify", headers=world.h,
                          json={"razorpay_order_id": order["razorpay_order_id"], "razorpay_payment_id": authorised["id"]})
    assert r.json()["status"] == "created"
    assert await credits.bought_remaining(session, world.club.id, "whatsapp") == 0

    rzp.payments[authorised["id"]]["status"] = "captured"
    event = {"entity": "event", "event": "order.paid", "payload": {
        "payment": {"entity": {**authorised, "status": "captured"}}, "order": {"entity": {"id": order["razorpay_order_id"]}}}}
    await payments.handle_razorpay_webhook(session, event, event_id="evt_cr_2", signature="sig")
    assert await credits.bought_remaining(session, world.club.id, "whatsapp") == 100
    # A second event for the same payment (payment.captured after order.paid) adds nothing.
    again = {"entity": "event", "event": "payment.captured", "payload": {"payment": {"entity": {**authorised, "status": "captured"}}}}
    await payments.handle_razorpay_webhook(session, again, event_id="evt_cr_3", signature="sig")
    assert await credits.bought_remaining(session, world.club.id, "whatsapp") == 100


async def test_the_sweep_finds_a_payment_whose_webhook_never_came(client, session, world, rzp):
    await _seed_packs(session)
    order = await _buy(client, world, (await _pack(session)).id)
    rzp.pay(order["razorpay_order_id"])
    # Too soon: the checkout may still be open.
    assert await credits.reconcile(session) == 0
    assert await credits.reconcile(session, _now() + timedelta(minutes=11)) == 1
    assert await credits.bought_remaining(session, world.club.id, "email") == 1000
    assert await credits.reconcile(session, _now() + timedelta(minutes=12)) == 0


async def test_buying_is_refused_off_sale_without_a_key_and_for_another_clubs_order(client, session, world, rzp, monkeypatch,
                                                                                     make_club, mint_token):
    await _seed_packs(session)
    pack = await _pack(session)
    pack.active = False
    await session.commit()
    r = await client.post(f"{API}/clubs/messages/credits/orders", json={"pack_id": pack.id}, headers=world.h)
    assert r.status_code == 404 and r.json()["error_code"] == "PACK_NOT_FOUND"
    assert (await client.get(f"{API}/credit-packs")).json()["packs"][0]["quantity"] == 5000

    other = await _pack(session, "email", 5000)
    order = await _buy(client, world, other.id)
    payment = rzp.pay(order["razorpay_order_id"])
    stranger_owner, _ = await make_club(name="Other Club", email="other-credit@example.com")
    stranger_h = auth_header(await mint_token(stranger_owner))
    r = await client.post(f"{API}/clubs/messages/credits/verify", headers=stranger_h,
                          json={"razorpay_order_id": order["razorpay_order_id"], "razorpay_payment_id": payment["id"]})
    assert r.status_code == 404
    # A payment for another order is not this one's.
    stray = rzp.pay((await _buy(client, world, other.id))["razorpay_order_id"])
    r = await client.post(f"{API}/clubs/messages/credits/verify", headers=world.h,
                          json={"razorpay_order_id": order["razorpay_order_id"], "razorpay_payment_id": stray["id"]})
    assert r.status_code == 400 and r.json()["error_code"] == "PAYMENT_ORDER_MISMATCH"

    monkeypatch.setattr(settings, "RAZORPAY_KEY_ID", "")
    r = await client.post(f"{API}/clubs/messages/credits/orders", json={"pack_id": other.id}, headers=world.h)
    assert r.status_code == 503 and r.json()["error_code"] == "CHECKOUT_UNAVAILABLE"
    assert (await client.get(f"{API}/clubs/messages/credits", headers=world.h)).json()["can_buy"] is False


# ─── Refunds ─────────────────────────────────────────────────────────────


async def _bought(client, session, world, rzp, quantity=1000) -> dict:
    await _seed_packs(session)
    order = await _buy(client, world, (await _pack(session, "email", quantity)).id)
    payment = rzp.pay(order["razorpay_order_id"])
    r = await client.post(f"{API}/clubs/messages/credits/verify", headers=world.h,
                          json={"razorpay_order_id": order["razorpay_order_id"], "razorpay_payment_id": payment["id"]})
    return r.json()


async def test_a_refund_takes_back_the_unused_credits_and_gives_back_their_share(client, session, world, rzp, make_admin, mint_token):
    bought = await _bought(client, session, world, rzp)
    assert await _send(session, world.club.id, n=3) == 0  # Free's 3
    lot = await session.scalar(select(MessageCreditLots))
    lot.remaining = 750  # 250 of them sent
    await session.commit()
    admin = auth_header(await mint_token(await make_admin(email="credits-admin@example.com")))

    r = await client.post(f"{API}/admin/messages/credits/purchases/{bought['id']}/refund", headers=admin,
                          json={"reason": "Club closed down"})
    assert r.status_code == 200, r.text
    out = r.json()
    # 750 of 1,000 unused: 75% of ₹99, rounded down to the paisa.
    assert (out["status"], out["refunded_paise"], out["credits_remaining"]) == ("refunded", 7425, 0)
    assert rzp.of("payment.refund") == [("payment.refund", bought["razorpay_payment_id"], 7425)]
    assert await credits.bought_remaining(session, world.club.id, "email") == 0
    assert await _send(session, world.club.id) == 1
    audit = await session.scalar(select(AdminAuditLogs).where(AdminAuditLogs.action == "messages.credits_refunded"))
    assert audit.details["credits_taken_back"] == 750 and audit.details["reason"] == "Club closed down"
    # Never twice.
    again = await client.post(f"{API}/admin/messages/credits/purchases/{bought['id']}/refund", headers=admin, json={"reason": "again"})
    assert again.status_code == 409 and again.json()["error_code"] == "NOT_REFUNDABLE"
    view = (await client.get(f"{API}/admin/messages/clubs/{world.club.id}/credits", headers=admin)).json()
    assert [lot["status"] for lot in view["lots"]] == ["refunded"]
    assert view["purchases"][0]["refund_reason"] == "Club closed down"


async def test_a_refund_razorpay_refused_or_never_answered_is_never_made_twice(client, session, world, rzp, make_admin, mint_token):
    bought = await _bought(client, session, world, rzp)
    admin = auth_header(await mint_token(await make_admin(email="credits-admin2@example.com")))
    url = f"{API}/admin/messages/credits/purchases/{bought['id']}/refund"

    rzp.refund_error = BadRequestError("The refund amount is invalid")
    r = await client.post(url, headers=admin, json={"reason": "Bought twice by mistake", "amount_paise": 5000})
    assert r.status_code == 502 and r.json()["error_code"] == "RAZORPAY_ERROR"
    row = await _fresh(session, MessageCreditPurchases, bought["id"])
    assert (row.status, row.refund_amount_paise) == ("refund_failed", 5000)
    # The credits went with the ask.
    assert await credits.bought_remaining(session, world.club.id, "email") == 0

    # Made, but the answer was lost: found, not made again.
    rzp.refund_error = "made-then-timeout"
    r = await client.post(url, headers=admin, json={"reason": "Bought twice by mistake"})
    assert r.status_code == 200 and r.json()["refunded_paise"] == 5000
    assert rzp.payments[bought["razorpay_payment_id"]]["amount_refunded"] == 5000
    assert len(rzp.of("payment.refund")) == 2

    bad = await client.post(f"{API}/admin/messages/credits/purchases/{bought['id']}/refund", headers=admin,
                            json={"reason": "more", "amount_paise": 1})
    assert bad.status_code == 409


async def test_a_refund_being_made_is_not_asked_for_again(client, session, world, rzp, make_admin, mint_token):
    """Two admins at once: the second is told to wait, and Razorpay is asked once."""
    bought = await _bought(client, session, world, rzp)
    row = await _fresh(session, MessageCreditPurchases, bought["id"])
    row.status, row.refund_amount_paise, row.refund_requested_at = "refunding", 9900, _now()
    await session.commit()
    admin = auth_header(await mint_token(await make_admin(email="credits-admin4@example.com")))
    r = await client.post(f"{API}/admin/messages/credits/purchases/{bought['id']}/refund", headers=admin,
                          json={"reason": "Clicked twice"})
    assert r.status_code == 409 and r.json()["error_code"] == "REFUND_IN_PROGRESS"
    assert rzp.of("payment.refund") == []


async def test_a_refund_made_in_razorpays_dashboard_takes_the_credits_back(client, session, world, rzp):
    bought = await _bought(client, session, world, rzp)
    rzp.payments[bought["razorpay_payment_id"]]["amount_refunded"] = 9900
    event = {"entity": "event", "event": "refund.processed", "payload": {
        "refund": {"entity": {"id": "rfnd_dash", "payment_id": bought["razorpay_payment_id"], "amount": 9900}}}}
    await payments.handle_razorpay_webhook(session, event, event_id="evt_cr_refund", signature="sig")
    row = await _fresh(session, MessageCreditPurchases, bought["id"])
    assert (row.status, row.refunded_paise, row.razorpay_refund_id) == ("refunded", 9900, "rfnd_dash")
    assert await credits.bought_remaining(session, world.club.id, "email") == 0


async def test_the_refund_amount_is_checked(client, session, world, rzp, make_admin, mint_token):
    bought = await _bought(client, session, world, rzp)
    admin = auth_header(await mint_token(await make_admin(email="credits-admin3@example.com")))
    url = f"{API}/admin/messages/credits/purchases/{bought['id']}/refund"
    r = await client.post(url, headers=admin, json={"reason": "too much", "amount_paise": 9901})
    assert r.status_code == 422 and r.json()["error_code"] == "BAD_AMOUNT"
    assert (await client.post(url, headers=admin, json={"reason": "  "})).status_code in (400, 422)
    assert rzp.of("payment.refund") == []
    assert await credits.bought_remaining(session, world.club.id, "email") == 1000


# ─── Grants and packs (admin) ────────────────────────────────────────────


async def test_an_admin_grants_free_credits_with_a_reason(client, session, world, make_admin, mint_token):
    from app.api.users.models import AdminRoles

    admin_user = await make_admin(email="credits-granter@example.com")
    admin = auth_header(await mint_token(admin_user))
    support = auth_header(await mint_token(await make_admin(email="credits-support@example.com", role=AdminRoles.support)))
    url = f"{API}/admin/messages/clubs/{world.club.id}/credits/grants"
    body = {"channel": "whatsapp", "quantity": 250, "reason": "Sorry for Tuesday's outage", "valid_months": 6}
    assert (await client.post(url, json=body, headers=support)).status_code == 403
    assert (await client.post(url, json={**body, "reason": "no"}, headers=admin)).status_code in (400, 422)
    r = await client.post(url, json=body, headers=admin)
    assert r.status_code == 200, r.text
    [lot] = r.json()["lots"]
    assert (lot["source"], lot["quantity"], lot["remaining"], lot["status"], lot["reason"]) == (
        "grant", 250, 250, "active", "Sorry for Tuesday's outage")
    assert lot["granted_by"]["id"] == admin_user.id
    audit = await session.scalar(select(AdminAuditLogs).where(AdminAuditLogs.action == "messages.credits_granted"))
    assert audit.details["quantity"] == 250 and audit.details["reason"] == "Sorry for Tuesday's outage"
    # The club sees it, without the admins' reason, and is told in its bell.
    mine = (await client.get(f"{API}/clubs/messages/credits", headers=world.h)).json()
    [seen] = mine["channels"]["whatsapp"]["lots"]
    assert (seen["source"], seen["reason"]) == ("grant", None)
    assert mine["channels"]["whatsapp"]["bought"]["remaining"] == 250
    kinds = [a["kind"] for a in (await client.get(f"{API}/clubs/alerts", headers=world.h)).json()["items"]]
    assert kinds == ["message_credits_added"]


async def test_admins_edit_the_packs(client, session, make_admin, mint_token):
    from app.api.users.models import AdminRoles

    await _seed_packs(session)
    admin = auth_header(await mint_token(await make_admin(email="credits-packs@example.com")))
    support = auth_header(await mint_token(await make_admin(email="credits-packs-support@example.com", role=AdminRoles.support)))
    listed = (await client.get(f"{API}/admin/messages/packs", headers=support)).json()
    assert len(listed["packs"]) == 6 and listed["prices_include_gst"] is True
    first = listed["packs"][0]
    assert (await client.patch(f"{API}/admin/messages/packs/{first['id']}", json={"price_paise": 8900}, headers=support)).status_code == 403
    r = await client.patch(f"{API}/admin/messages/packs/{first['id']}", json={"price_paise": 8900, "active": False}, headers=admin)
    assert r.status_code == 200 and (r.json()["price_paise"], r.json()["active"]) == (8900, False)
    made = await client.post(f"{API}/admin/messages/packs", headers=admin,
                             json={"channel": "whatsapp", "quantity": 10000, "price_paise": 1199900, "sort_order": 40})
    assert made.status_code == 200 and made.json()["active"] is True
    assert (await client.post(f"{API}/admin/messages/packs", headers=admin,
                              json={"channel": "sms", "quantity": 1, "price_paise": 100})).status_code in (400, 422)
    public = (await client.get(f"{API}/credit-packs")).json()["packs"]
    assert [(p["channel"], p["quantity"]) for p in public] == [
        ("email", 5000), ("email", 20000), ("whatsapp", 100), ("whatsapp", 500), ("whatsapp", 2000), ("whatsapp", 10000)]
    actions = list(await session.scalars(select(AdminAuditLogs.action).where(AdminAuditLogs.action.like("messages.pack_%"))))
    assert sorted(actions) == ["messages.pack_created", "messages.pack_updated"]


# ─── Telling the club ────────────────────────────────────────────────────


async def test_the_club_is_told_once_when_its_credits_run_low(client, session, world, mail):  # noqa: F811
    club_id = world.club.id
    await _send(session, club_id, n=3)  # Free's allowance
    lot = await _lot(session, club_id, quantity=100, remaining=11, source="purchase")
    assert await _send(session, club_id) == 0  # 10 left: 10%, not under it
    assert await session.scalar(select(func.count(ClubAlerts.id)).where(ClubAlerts.kind == "message_credits_low")) == 0
    assert await _send(session, club_id, n=3) == 0  # 7 left
    assert (await _fresh(session, MessageCreditLots, lot.id)).low_warned_at is not None
    alerts = list(await session.scalars(select(ClubAlerts).where(ClubAlerts.kind == "message_credits_low")))
    assert len(alerts) == 1
    assert alerts[0].title == "9 email credits left" and alerts[0].link == "/billing#credits"
    assert alerts[0].data["action_label"] == "Buy credits"
    await mail.drain()
    assert [m["subject"] for m in mail.to("credit-owner@example.com") if "credits left" in m["subject"]] == ["9 email credits left"]
    # A new pack: told again when that one runs low.
    await _lot(session, club_id, quantity=10, source="purchase")
    assert await _send(session, club_id, n=16) == 0  # 1 left of 17: not under 10% of 10
    assert await session.scalar(select(func.count(ClubAlerts.id)).where(ClubAlerts.kind == "message_credits_low")) == 1
    assert await _send(session, club_id) == 0
    assert await session.scalar(select(func.count(ClubAlerts.id)).where(ClubAlerts.kind == "message_credits_low")) == 2


async def test_the_club_is_told_a_month_before_credits_expire(client, session, world, mail):  # noqa: F811
    club_id = world.club.id
    soon = await _lot(session, club_id, quantity=500, remaining=120, expires_in=20, bought_ago=345)
    await _lot(session, club_id, quantity=500, expires_in=40)
    await _lot(session, club_id, quantity=500, remaining=0, expires_in=10)
    assert await credits.warn_expiring(session) == 1
    assert await credits.warn_expiring(session) == 0
    [alert] = list(await session.scalars(select(ClubAlerts).where(ClubAlerts.kind == "message_credits_expiring")))
    assert alert.title.startswith("120 email credits expire on ") and alert.data["lot_id"] == soon.id
    await mail.drain()
    assert len([m for m in mail.to("credit-owner@example.com") if "expire on" in m["subject"]]) == 1


# ─── Overage billing, retired ────────────────────────────────────────────


@pytest.mark.parametrize("flag", [True, False, "unset"])
async def test_nothing_is_charged_on_1_november_whatever_the_old_flag_says(session, world, monkeypatch, flag):
    """October past the limits on a plan Razorpay bills: the first sweep of
    November used to add the extra to the club's next invoice. Now it makes
    no statement and asks Razorpay nothing, with the old flag on, off, or
    not set at all (its default)."""
    from app.api.payments import service as payments_service
    from app.api.subscriptions import billing as subs_billing
    from app.api.subscriptions import sweep

    fake = FakeSubscriptions()
    monkeypatch.setattr(subs_billing, "razorpay_client", fake)
    monkeypatch.setattr(payments_service, "razorpay_client", fake)
    if flag != "unset":
        monkeypatch.setattr(settings, "MESSAGE_OVERAGE_BILLING", flag)
    club_id = world.club.id
    await _paying(session, club_id, world.growth.id)
    october = datetime(2026, 10, 20, 12, 0, tzinfo=IST)
    for kind in ("ticket",) * 6:
        await quota.record_message(club_id, "email", kind, session=session, at=october)
    await session.commit()
    row = await session.scalar(select(MessageUsage).where(MessageUsage.club_id == club_id))
    assert (row.used, row.plan_used, row.uncovered) == (6, 3, 3)

    done = await sweep.sweep(session, now=datetime(2026, 11, 1, 0, 30, tzinfo=IST))
    assert (done["message_statements"], done["overage_charged"], done["overage_failed"]) == (0, 0, 0)
    assert await session.scalar(select(func.count(MessageStatements.id))) == 0
    assert fake.of("subscription", "createAddon") == [] and fake.calls == []
    # October's month, in the club's history: what was sent, nothing charged.
    [month] = await quota.history(session, club_id, now=datetime(2026, 11, 2, tzinfo=IST))
    assert (month["period"], month["status"], month["amount_paise"]) == ("2026-10", "nothing_due", 0)
    assert (month["email"]["plan_used"], month["email"]["over"]) == (3, 3)


async def test_statements_left_open_are_void_and_charged_ones_stay(session, world):
    from tests.test_trial_pricing import _migration

    club_id = world.club.id
    periods = [datetime(2026, m, 1).date() for m in (3, 4, 5, 6, 7, 8, 9)]
    for period, status in zip(periods, ("pending", "failed", "unconfirmed", "gave_up", "manual", "charged", "charging")):
        session.add(MessageStatements(club_id=club_id, period=period, billing="razorpay", amount_paise=500, status=status,
                                      razorpay_addon_id="ao_kept" if status == "charged" else None))
    await session.commit()
    # The migration voids them once ...
    migration = _migration("message_credits")
    connection = await session.connection()
    assert await connection.run_sync(lambda sync: migration.void_open_statements(sync)) == 6
    await session.commit()
    statuses = dict((await session.execute(select(MessageStatements.period, MessageStatements.status))).all())
    assert sorted(statuses.values()) == ["charged", "void", "void", "void", "void", "void", "void"]
    # ... and the sweep makes sure of it.
    session.add(MessageStatements(club_id=club_id, period=datetime(2026, 2, 1).date(), billing="razorpay", amount_paise=500,
                                  status="pending"))
    await session.commit()
    assert (await billing.run(session))["statements_voided"] == 1
    voided = await session.scalar(select(MessageStatements).where(MessageStatements.status == "void").limit(1))
    assert voided.last_error == billing.VOIDED and voided.next_attempt_at is None
    assert [m["status"] for m in await quota.history(session, club_id)] == []  # no usage rows: nothing to show


async def test_the_old_retry_route_is_gone(client, session, world, make_admin, mint_token):
    admin = auth_header(await mint_token(await make_admin(email="credits-retry@example.com")))
    r = await client.post(f"{API}/admin/messages/statements/1/retry", headers=admin)
    assert r.status_code in (404, 405)


async def test_the_migration_splits_existing_counters(session, world):
    from tests.test_trial_pricing import _migration

    club_id = world.club.id
    period = quota.period_of()
    session.add(MessageUsage(club_id=club_id, period=period, channel="email", used=8, quota=5))
    session.add(MessageUsage(club_id=club_id, period=period, channel="whatsapp", used=4, quota=None))
    await session.commit()
    migration = _migration("message_credits")
    assert migration.down_revision is not None
    connection = await session.connection()
    assert await connection.run_sync(lambda sync: migration.split_usage(sync)) == 2
    assert await connection.run_sync(lambda sync: migration.split_usage(sync)) == 0
    await session.commit()
    email, wa = await _usage(session, club_id), await _usage(session, club_id, "whatsapp")
    assert (email.plan_used, email.uncovered, wa.plan_used, wa.uncovered) == (5, 3, 4, 0)
