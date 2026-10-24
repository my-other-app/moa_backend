"""Billing alerts: what the admins are told, what the club is mailed, and the
seven days of grace a paying club gets after Razorpay stops retrying.

Razorpay and the mail provider are fakes throughout: Razorpay through the
FakeRazorpay of test_club_billing, mail by patching `send_email`, Discord by
an httpx MockTransport.
"""
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import func, select, text

from app.api.users.models import AdminRoles
from tests.conftest import auth_header
from tests.test_club_billing import (  # noqa: F401  (razorpay and shop are fixtures)
    FakeRazorpay,
    _row,
    make_plan,
    razorpay,
    shop,
)

API = "/api/v1"
DAY = timedelta(days=1)


def _now():
    return datetime.now(timezone.utc)


# ─── Helpers ─────────────────────────────────────────────────────────────


@pytest.fixture
def mail(monkeypatch):
    """Every mail sent, as send_email's keyword arguments, once `await
    mail.drain()` has let the after-commit queue finish."""
    from app.core import after_commit
    from app.core.email import email as email_mod

    class Box(list):
        async def drain(self):
            await after_commit.drain()
            return self

        def to(self, address):
            return [m for m in self if m["recipients"] == [address]]

        def admin(self):
            return [m for m in self if " admin] " in m["subject"]]

    box = Box()
    monkeypatch.setattr(email_mod, "send_email", lambda **kw: box.append(kw))
    return box


async def _alerts(session, kind=None):
    from app.api.admin_alerts.models import AdminNotifications

    query = select(AdminNotifications).order_by(AdminNotifications.id).execution_options(populate_existing=True)
    if kind:
        query = query.where(AdminNotifications.kind == kind)
    return list((await session.execute(query)).scalars())


async def _kinds(session):
    return [a.kind for a in await _alerts(session)]


_seq = {"n": 0}


async def _hook(session, event, sub_id, *, payment=None, **entity):
    """A Razorpay webhook, as handle_razorpay_webhook receives it."""
    from app.api.payments.service import handle_razorpay_webhook

    _seq["n"] += 1
    payload = {"subscription": {"entity": {"id": sub_id, "notes": {}, **entity}}}
    if payment is not None:
        payload["payment"] = {"entity": payment}
    await handle_razorpay_webhook(
        session, {"event": event, "entity": "event", "payload": payload},
        event_id=f"evt_{_seq['n']}_{event}", signature="sig",
    )


async def _payment_failed(session, sub_id, *, pay_id, reason="Card declined"):
    from app.api.payments.service import handle_razorpay_webhook

    _seq["n"] += 1
    data = {"event": "payment.failed", "entity": "event", "payload": {"payment": {"entity": {
        "id": pay_id, "subscription_id": sub_id, "error_description": reason, "amount": 69900,
    }}}}
    await handle_razorpay_webhook(session, data, event_id=f"evt_{_seq['n']}_pf", signature="sig")


async def _paid(session, shop, *, status="active", sub_id="sub_paid", days_left=20, **extra):
    """A subscription Razorpay has charged before."""
    from app.api.subscriptions.models import ClubSubscriptions

    now = _now()
    row = ClubSubscriptions(
        club_id=shop["club"].id, plan_id=shop["growth"].id, status=status, razorpay_subscription_id=sub_id,
        authenticated_at=now - 40 * DAY, activated_at=now - 40 * DAY,
        current_period_end=now + days_left * DAY, mrr_paise=69900, **extra,
    )
    session.add(row)
    await session.commit()
    return row


async def _admin_header(make_admin, mint_token, role=None, email="alerts-admin@example.com"):
    return auth_header(await mint_token(await make_admin(email=email, role=role)))


def _age(alert, minutes):
    alert.created_at = alert.created_at - timedelta(minutes=minutes)


# ─── 1. Every kind, once ─────────────────────────────────────────────────


async def test_a_club_application_alerts_the_admins_with_the_plan_it_picked(client, session, mail, shop):
    response = await client.post(f"{API}/clubs/create", data={
        "email": "applicant@example.com", "password": "Password123!", "name": "Applying Club",
        "requested_plan": "growth", "requested_cycle": "yearly",
    })
    assert response.status_code == 200, response.text

    [alert] = await _alerts(session, "club_applied")
    assert alert.club_id == response.json()["id"] and alert.subscription_id is None
    assert alert.data["requested_plan"] == "growth" and alert.data["requested_cycle"] == "yearly"
    assert "Applying Club" in alert.title and alert.read_at is None

    await mail.drain()
    [sent] = mail.admin()
    assert sent["recipients"] == ["support@myotherapp.in"]
    assert "Applying Club" in sent["subject"] and "growth" in sent["text"]


async def test_a_trial_then_its_first_charge_then_renewals(client, session, shop, razorpay, mail):
    from app.api.subscriptions.models import SubscriptionEmails

    sub_id = (await client.post(f"{API}/clubs/subscription/start", json={"plan_key": "growth"}, headers=shop["h"])).json()["razorpay_subscription_id"]

    await _hook(session, "subscription.authenticated", sub_id)
    await _hook(session, "subscription.authenticated", sub_id)  # delivered again
    assert await _kinds(session) == ["subscription_trial_started"]

    end = int((_now() + 30 * DAY).timestamp())
    await _hook(session, "subscription.activated", sub_id, current_end=end, paid_count=1)
    await _hook(session, "subscription.charged", sub_id, current_end=end, paid_count=1,
                payment={"id": "pay_first", "amount": 69900})
    assert await _kinds(session) == ["subscription_trial_started", "subscription_activated"]

    await _hook(session, "subscription.charged", sub_id, current_end=end, paid_count=2,
                payment={"id": "pay_2", "amount": 69900})
    [received] = await _alerts(session, "payment_received")
    assert received.data["amount_paise"] == 69900 and "₹699" in received.title

    # A second renewal inside ten minutes is the same alert; later, a new one.
    await _hook(session, "subscription.charged", sub_id, current_end=end, paid_count=3)
    assert len(await _alerts(session, "payment_received")) == 1
    _age(received, 11)
    await session.commit()
    await _hook(session, "subscription.charged", sub_id, current_end=end, paid_count=4)
    assert len(await _alerts(session, "payment_received")) == 2

    # The club got one welcome, at the trial's start, and nothing for receipts.
    await mail.drain()
    club_mail = mail.to("billing-owner@example.com")
    assert [m["subject"] for m in club_mail] == ["Welcome to Growth"]
    assert "trial" in club_mail[0]["text"] and "receipts" in club_mail[0]["text"]
    assert await session.scalar(select(func.count(SubscriptionEmails.id))) == 1


async def test_activation_without_a_trial_welcomes_once(client, session, shop, razorpay, mail):
    from app.api.subscriptions.models import ClubSubscriptions

    session.add(ClubSubscriptions(club_id=shop["club"].id, plan_id=shop["growth"].id, status="cancelled",
                                  trial_started_at=_now() - 90 * DAY))  # the trial was used before
    await session.commit()
    sub_id = (await client.post(f"{API}/clubs/subscription/start", json={"plan_key": "pro"}, headers=shop["h"])).json()["razorpay_subscription_id"]
    await _hook(session, "subscription.authenticated", sub_id)
    assert await _kinds(session) == []

    end = int((_now() + 30 * DAY).timestamp())
    await _hook(session, "subscription.charged", sub_id, current_end=end, paid_count=1)
    await _hook(session, "subscription.activated", sub_id, current_end=end)
    assert await _kinds(session) == ["subscription_activated"]
    await mail.drain()
    [welcome] = mail.to("billing-owner@example.com")
    assert welcome["subject"] == "Welcome to Pro" and "paid through" in welcome["text"]


async def test_a_failed_renewal_alerts_once_and_mails_the_club_once(session, shop, mail):
    await _paid(session, shop)

    await _payment_failed(session, "sub_paid", pay_id="pay_f1")
    await _hook(session, "subscription.pending", "sub_paid", short_url="https://rzp.io/i/upd")
    assert await _kinds(session) == ["payment_failed", "subscription_past_due"]
    [failed] = await _alerts(session, "payment_failed")
    assert failed.data["reason"] == "Card declined" and failed.data["payment_id"] == "pay_f1"

    await mail.drain()
    [club_mail] = mail.to("billing-owner@example.com")
    assert club_mail["subject"] == "Your payment for Growth didn't go through"
    assert "Card declined" in club_mail["text"] and "keeps Growth" in club_mail["text"]
    # No short_url on a payment event: the club is sent to Billing.
    assert "/billing" in club_mail["text"]

    # Razorpay's next retry failing, a day on, is a new failure.
    from app.api.subscriptions.models import SubscriptionEmails

    for row in (await session.execute(select(SubscriptionEmails))).scalars():
        row.created_at -= DAY
    for alert in await _alerts(session):
        _age(alert, 24 * 60)
    await session.commit()
    mail.clear()
    await _payment_failed(session, "sub_paid", pay_id="pay_f2", reason="Insufficient funds")
    await mail.drain()
    assert len(mail.to("billing-owner@example.com")) == 1
    assert len(await _alerts(session, "payment_failed")) == 2
    assert len(await _alerts(session, "subscription_past_due")) == 1  # it was already past due


async def test_a_pending_failure_mails_the_update_link_razorpay_gives(session, shop, mail):
    await _paid(session, shop)
    await _hook(session, "subscription.pending", "sub_paid", short_url="https://rzp.io/i/upd")
    await mail.drain()
    [club_mail] = mail.to("billing-owner@example.com")
    assert "https://rzp.io/i/upd" in club_mail["text"] and "https://rzp.io/i/upd" in club_mail["html"]


async def test_a_failed_first_charge_alerts_but_does_not_mail_the_club(client, session, shop, razorpay, mail):
    sub_id = (await client.post(f"{API}/clubs/subscription/start", json={"plan_key": "growth"}, headers=shop["h"])).json()["razorpay_subscription_id"]
    await _hook(session, "subscription.halted", sub_id, payment={"error_description": "Insufficient funds"})
    [alert] = await _alerts(session)
    assert alert.kind == "payment_failed" and alert.data["checkout"] is True
    await mail.drain()
    assert mail.to("billing-owner@example.com") == []


async def test_paused_resumed_and_cancelled_at_razorpay(session, shop):
    await _paid(session, shop)
    await _hook(session, "subscription.paused", "sub_paid")
    await _hook(session, "subscription.paused", "sub_paid")
    await _hook(session, "subscription.resumed", "sub_paid")
    await _hook(session, "subscription.resumed", "sub_paid")  # nothing to resume
    await _hook(session, "subscription.cancelled", "sub_paid")
    assert await _kinds(session) == ["subscription_paused", "subscription_resumed", "subscription_cancelled"]
    [cancelled] = await _alerts(session, "subscription_cancelled")
    assert cancelled.data["by"] == "razorpay"


async def test_a_completed_subscription_is_an_alert_too(session, shop):
    await _paid(session, shop)
    await _hook(session, "subscription.completed", "sub_paid")
    [alert] = await _alerts(session, "subscription_cancelled")
    assert alert.data["by"] == "razorpay_completed" and "completed" in alert.title


async def test_the_club_cancelling_alerts_once_not_again_when_razorpay_confirms(client, session, shop, razorpay):
    await _paid(session, shop)
    assert (await client.post(f"{API}/clubs/subscription/cancel", headers=shop["h"])).status_code == 200
    assert (await client.post(f"{API}/clubs/subscription/cancel", headers=shop["h"])).status_code == 200
    await _hook(session, "subscription.cancelled", "sub_paid")
    [alert] = await _alerts(session, "subscription_cancelled")
    assert alert.data["by"] == "club" and alert.club_id == shop["club"].id


async def test_an_unpaid_checkout_closing_is_not_a_cancellation(client, session, shop, razorpay):
    sub_id = (await client.post(f"{API}/clubs/subscription/start", json={"plan_key": "growth"}, headers=shop["h"])).json()["razorpay_subscription_id"]
    assert (await client.post(f"{API}/clubs/subscription/cancel", headers=shop["h"])).status_code == 200
    await _hook(session, "subscription.cancelled", sub_id, notes={"club_id": str(shop["club"].id)})
    assert await _kinds(session) == []


async def test_an_admin_moving_a_club_alerts_every_time(client, session, shop, make_admin, mint_token):
    header = await _admin_header(make_admin, mint_token)
    for plan in ("growth", "pro"):
        response = await client.put(f"{API}/admin/subscriptions/club/{shop['club'].id}/plan",
                                    json={"plan_id": shop[plan].id, "cycle": "monthly", "complimentary": False},
                                    headers=header)
        assert response.status_code == 200, response.text
    alerts = await _alerts(session, "plan_assigned")
    assert [a.data["plan"] for a in alerts] == ["growth", "pro"]
    assert alerts[0].data["by"] == "alerts-admin@example.com"


async def test_an_alert_that_cannot_be_written_never_fails_the_caller(session, shop):
    from app.api.admin_alerts import notify

    nothing = await notify(session, "x" * 80, title="Too long a kind for its column", club=shop["club"])
    assert nothing is None
    # The transaction is still usable, and a good alert still lands.
    assert await notify(session, "plan_assigned", title="Fine", club=shop["club"]) is not None
    await session.commit()
    assert await _kinds(session) == ["plan_assigned"]


async def test_an_alert_rolled_back_with_its_change_is_never_sent(_engine, mail):
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.api.admin_alerts import notify
    from app.db.core import SESSION_KWARGS

    # A session of its own: the suite's shared one cannot roll back for real.
    async with _engine.connect() as conn:
        async with AsyncSession(bind=conn, **SESSION_KWARGS) as own:
            assert await notify(own, "plan_assigned", title="Never happened") is not None
            await own.rollback()
            # The queue went with the rollback: a later commit sends nothing.
            await own.commit()
    await mail.drain()
    assert mail.admin() == []


async def test_a_savepoint_rolled_back_takes_only_its_own_mail_with_it(session):
    from app.core import after_commit

    sent = []
    after_commit.run_after_commit(session, sent.append, "before")
    with pytest.raises(RuntimeError):
        async with session.begin_nested():
            after_commit.run_after_commit(session, sent.append, "inside a failed savepoint")
            raise RuntimeError("the audit line could not be written")
    async with session.begin_nested():
        after_commit.run_after_commit(session, sent.append, "inside a released savepoint")
    await session.commit()
    await after_commit.drain()
    assert sent == ["before", "inside a released savepoint"]


# ─── 2. Recipients and throttles ─────────────────────────────────────────


async def test_admin_mail_goes_to_every_listed_address(session, shop, mail, monkeypatch):
    from app.api.admin_alerts import notify, service
    from app.config import settings

    monkeypatch.setattr(settings, "ADMIN_ALERT_EMAILS", " Ops@Example.com, money@example.com;ops@example.com, ,nonsense")
    assert service.recipients() == ["ops@example.com", "money@example.com"]

    await notify(session, "payment_failed", title="Payment failed for Billing Club (Growth)", body="Card declined",
                 club=shop["club"], data={"reason": "Card declined"})
    await session.commit()
    await mail.drain()
    [sent] = mail
    assert sent["recipients"] == ["ops@example.com", "money@example.com"]
    assert sent["subject"] == "[MyOtherApp admin] Payment failed for Billing Club (Growth)"
    assert "Billing Club" in sent["text"] and "Card declined" in sent["html"]


async def test_no_listed_address_means_no_admin_mail(session, shop, mail, monkeypatch):
    from app.api.admin_alerts import notify
    from app.config import settings

    monkeypatch.setattr(settings, "ADMIN_ALERT_EMAILS", "")
    await notify(session, "plan_assigned", title="Quiet", club=shop["club"])
    await session.commit()
    await mail.drain()
    assert mail == [] and await _kinds(session) == ["plan_assigned"]


def test_admin_mail_is_throttled(monkeypatch):
    from app.api.admin_alerts import service
    from app.core.email import email as email_mod

    sent = []
    monkeypatch.setattr(email_mod, "send_email", lambda **kw: sent.append(kw))
    for i in range(service.EMAIL_THROTTLE.limit + 5):
        service.send_email({"id": i, "kind": "payment_failed", "title": f"Alert {i}", "body": ""})
    assert len(sent) == service.EMAIL_THROTTLE.limit


def test_discord_posts_are_throttled_and_never_raise(monkeypatch):
    from app.api.admin_alerts import service
    from app.config import settings

    posts = []

    def handler(request):
        posts.append(request)
        return httpx.Response(204)

    real_client = httpx.Client
    monkeypatch.setattr(settings, "ADMIN_ALERT_DISCORD_WEBHOOK", "https://discord.test/api/webhooks/1/x")
    monkeypatch.setattr(service.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    for i in range(service.DISCORD_THROTTLE.limit + 3):
        service.post_discord({"id": i, "kind": "payment_failed", "title": f"Alert {i}", "body": "x", "club_id": 7, "club_name": "C"})
    assert len(posts) == service.DISCORD_THROTTLE.limit
    assert b"Alert 0" in posts[0].content and b'"parse":[]' in posts[0].content.replace(b" ", b"")

    service.reset_for_tests()

    def down(request):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(service.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(down), **kw))
    assert service.post_discord({"id": 1, "kind": "k", "title": "t", "body": ""}, retry_delay=0) is False


def test_no_discord_webhook_means_no_post(monkeypatch):
    from app.api.admin_alerts import service
    from app.config import settings

    monkeypatch.setattr(settings, "ADMIN_ALERT_DISCORD_WEBHOOK", None)
    monkeypatch.setattr(service.httpx, "Client", lambda **kw: (_ for _ in ()).throw(AssertionError("posted")))
    assert service.post_discord({"id": 1, "kind": "k", "title": "t", "body": ""}) is False


async def test_club_mail_goes_to_the_owners_sign_in_address(session, shop, mail):
    shop["club"].contact_email = "committee@example.com"
    await session.commit()
    await _paid(session, shop)
    await _payment_failed(session, "sub_paid", pay_id="pay_x")
    await mail.drain()
    assert len(mail.to("billing-owner@example.com")) == 1
    assert mail.to("committee@example.com") == []


# ─── 3. Grace ────────────────────────────────────────────────────────────


async def test_halting_a_paid_subscription_starts_seven_days_of_grace(client, session, shop, mail):
    from app.api.subscriptions import service

    await _paid(session, shop)
    await _payment_failed(session, "sub_paid", pay_id="pay_last")
    await _hook(session, "subscription.halted", "sub_paid", short_url="https://rzp.io/i/fix")

    row = await _row(session, shop["club"].id)
    assert row.status.value == "past_due" and row.halted_at is not None
    assert row.grace_ends_at == row.halted_at + timedelta(days=service.GRACE_DAYS)
    assert service.in_grace(row) and service.is_entitled(row)
    assert (await service.resolve_entitlements(session, shop["club"].id)).plan.key == "growth"

    assert await _kinds(session) == ["payment_failed", "subscription_past_due", "grace_started"]
    await mail.drain()
    club_mail = mail.to("billing-owner@example.com")
    assert [m["subject"].split(" to keep")[0] for m in club_mail] == [
        "Your payment for Growth didn't go through", f"Update your payment by {_day(row.grace_ends_at)}"]
    assert "https://rzp.io/i/fix" in club_mail[1]["text"] and "Free plan" in club_mail[1]["text"]

    mine = (await client.get(f"{API}/clubs/subscription", headers=shop["h"])).json()
    assert mine["halted_at"] is not None and mine["grace_ends_at"] is not None and mine["in_grace"] is True

    # Halted again (a stray redelivery): no second grace, no second mail.
    await _hook(session, "subscription.halted", "sub_paid")
    assert len(await _alerts(session, "grace_started")) == 1


def _day(value):
    from app.api.subscriptions.notices import day

    return day(value)


async def test_halting_what_was_never_paid_gives_no_grace(session, shop):
    from app.api.subscriptions.models import ClubSubscriptions

    session.add(ClubSubscriptions(club_id=shop["club"].id, plan_id=shop["growth"].id, status="trial",
                                  razorpay_subscription_id="sub_trial", authenticated_at=_now() - 14 * DAY,
                                  trial_started_at=_now() - 14 * DAY))
    await session.commit()
    await _hook(session, "subscription.halted", "sub_trial")
    row = await _row(session, shop["club"].id)
    assert row.status.value == "past_due" and row.halted_at is None and row.grace_ends_at is None
    assert "grace_started" not in await _kinds(session)


@pytest.mark.parametrize("event", ["subscription.charged", "subscription.resumed", "subscription.activated"])
async def test_paying_resuming_or_activating_clears_the_grace(session, shop, event):
    await _paid(session, shop)
    await _hook(session, "subscription.halted", "sub_paid")
    assert (await _row(session, shop["club"].id)).grace_ends_at is not None

    await _hook(session, event, "sub_paid", current_end=int((_now() + 30 * DAY).timestamp()), paid_count=3)

    row = await _row(session, shop["club"].id)
    assert row.status.value == "active"
    assert (row.halted_at, row.grace_ends_at, row.grace_reminded_at) == (None, None, None)


async def test_access_ends_when_the_grace_does_even_before_the_sweep(client, session, shop):
    from app.api.subscriptions import service

    now = _now()
    await _paid(session, shop, status="past_due", halted_at=now - 8 * DAY, grace_ends_at=now - DAY)
    row = await _row(session, shop["club"].id)
    assert service.is_entitled(row) is False
    assert service.is_entitled(row, now - 2 * DAY) is True  # a day before the end it still stood

    ent = (await client.get(f"{API}/clubs/entitlements", headers=shop["h"])).json()
    assert ent["plan_key"] == "free" and ent["subscription_status"] == "past_due"
    mine = (await client.get(f"{API}/clubs/subscription", headers=shop["h"])).json()
    assert mine["entitled"] is False and mine["in_grace"] is False and mine["grace_ends_at"] is not None


async def test_the_sweep_reminds_two_days_before_and_only_once(session, shop, razorpay, mail):
    from app.api.subscriptions import sweep

    await _paid(session, shop)
    await _hook(session, "subscription.halted", "sub_paid")
    row = await _row(session, shop["club"].id)
    ends = row.grace_ends_at

    assert (await sweep.sweep(session, now=ends - 3 * DAY))["grace_reminded"] == 0
    assert (await sweep.sweep(session, now=ends - timedelta(hours=47)))["grace_reminded"] == 1
    assert (await sweep.sweep(session, now=ends - timedelta(hours=30)))["grace_reminded"] == 0

    row = await _row(session, shop["club"].id)
    assert row.grace_reminded_at is not None and row.status.value == "past_due"
    [ending] = await _alerts(session, "grace_ending")
    assert _day(ends) in ending.title
    await mail.drain()
    reminders = [m for m in mail.to("billing-owner@example.com") if m["subject"].startswith("Two days left")]
    assert len(reminders) == 1 and _day(ends) in reminders[0]["subject"]


async def test_the_sweep_moves_a_club_to_free_when_its_grace_runs_out(client, session, shop, razorpay, mail):
    from app.api.subscriptions import service, sweep

    await _paid(session, shop)
    await _hook(session, "subscription.halted", "sub_paid")
    ends = (await _row(session, shop["club"].id)).grace_ends_at

    done = await sweep.sweep(session, now=ends + timedelta(hours=1))

    assert done["downgraded"] == 1
    assert razorpay.of("subscription", "cancel") == [("sub_paid", {"cancel_at_cycle_end": 0})]
    row = await _row(session, shop["club"].id)
    assert row.status.value == "cancelled" and row.mrr_paise == 0 and row.cancel_requested_at is not None
    assert (await service.resolve_entitlements(session, shop["club"].id)).plan.key == "free"
    [down] = await _alerts(session, "subscription_downgraded")
    assert down.data["outcome"] == "cancelled" and down.data["razorpay_cancelled"] is True
    await mail.drain()
    [moved] = [m for m in mail.to("billing-owner@example.com") if "Free plan" in m["subject"]]
    assert moved["subject"] == "Billing Club has moved to the Free plan"

    # Razorpay's own cancelled arriving afterwards adds nothing, and a second sweep does nothing.
    await _hook(session, "subscription.cancelled", "sub_paid")
    assert (await sweep.sweep(session, now=ends + timedelta(hours=2)))["downgraded"] == 0
    assert "subscription_cancelled" not in await _kinds(session)


async def test_razorpay_refusing_the_cancel_still_ends_the_grace(session, shop, razorpay):
    from app.api.subscriptions import sweep

    def refuse(sub_id, data):
        raise RuntimeError("Razorpay down")

    razorpay.handlers[("subscription", "cancel")] = refuse
    now = _now()
    await _paid(session, shop, status="past_due", halted_at=now - 8 * DAY, grace_ends_at=now - DAY)

    assert (await sweep.sweep(session))["downgraded"] == 1
    assert (await _row(session, shop["club"].id)).status.value == "cancelled"
    [down] = await _alerts(session, "subscription_downgraded")
    assert down.data["razorpay_cancelled"] is False and "Razorpay" in down.body


async def test_a_lapsed_grace_goes_back_to_an_admin_grant_kept_in_previous_state(session, shop, razorpay):
    from app.api.subscriptions import service, sweep

    now = _now()
    await _paid(session, shop, status="past_due", halted_at=now - 8 * DAY, grace_ends_at=now - DAY,
                previous_state={"plan_id": shop["pro"].id, "cycle": "monthly", "status": "active",
                                "razorpay_subscription_id": None, "activated_at": (now - 300 * DAY).isoformat(),
                                "mrr_paise": 0, "cancel_at_period_end": False})

    await sweep.sweep(session)

    row = await _row(session, shop["club"].id)
    assert (row.status.value, row.razorpay_subscription_id) == ("active", None)
    assert (await service.resolve_entitlements(session, shop["club"].id)).plan.key == "pro"
    [down] = await _alerts(session, "subscription_downgraded")
    assert down.data["outcome"] == "restored"


async def test_the_sweep_mails_a_trial_ending_once(session, shop, mail):
    from app.api.subscriptions import sweep
    from app.api.subscriptions.models import ClubSubscriptions

    now = _now()
    session.add(ClubSubscriptions(club_id=shop["club"].id, plan_id=shop["growth"].id, status="trial",
                                  razorpay_subscription_id="sub_trial", authenticated_at=now - 12 * DAY,
                                  trial_started_at=now - 12 * DAY, trial_ends_at=now + 2 * DAY,
                                  current_period_end=now + 2 * DAY))
    await session.commit()

    assert (await sweep.sweep(session, now=now - 2 * DAY))["trial_reminded"] == 0
    assert (await sweep.sweep(session))["trial_reminded"] == 1
    assert (await sweep.sweep(session))["trial_reminded"] == 0

    await mail.drain()
    [reminder] = mail.to("billing-owner@example.com")
    assert reminder["subject"] == f"Your Growth trial ends on {_day(now + 2 * DAY)}"
    assert "₹699 a month" in reminder["text"]
    assert (await _row(session, shop["club"].id)).trial_reminded_at is not None


async def test_the_sweep_runs_on_one_worker_at_a_time(session, _engine, shop):
    from app.api.subscriptions import sweep

    other = await _engine.connect()
    try:
        await other.execute(text(f"SELECT pg_advisory_lock({sweep.SWEEP_LOCK})"))
        assert await sweep.run_sweep(session) is None
        await other.execute(text(f"SELECT pg_advisory_unlock({sweep.SWEEP_LOCK})"))
        assert await sweep.run_sweep(session) == {
            "downgraded": 0, "grace_reminded": 0, "trial_reminded": 0, "comp_ended": 0, "comp_reminded": 0,
            "free_trial_ended": 0, "free_trial_reminded": 0, "enterprise_trial_ended": 0, "enterprise_trial_reminded": 0,
            "message_statements": 0, "overage_charged": 0, "overage_failed": 0,
        }
        # And it lets go when done.
        assert await other.scalar(text(f"SELECT pg_try_advisory_lock({sweep.SWEEP_LOCK})")) is True
        await other.execute(text(f"SELECT pg_advisory_unlock({sweep.SWEEP_LOCK})"))
    finally:
        await other.rollback()
        await other.close()


# ─── 4. Extending the grace, and an admin cancelling ─────────────────────


async def _sub_id(session, shop):
    return (await _row(session, shop["club"].id)).id


async def test_extending_a_running_grace(client, session, shop, make_admin, mint_token):
    from app.api.audit.models import AdminAuditLogs

    await _paid(session, shop)
    await _hook(session, "subscription.halted", "sub_paid")
    before = (await _row(session, shop["club"].id)).grace_ends_at
    header = await _admin_header(make_admin, mint_token)

    response = await client.post(f"{API}/admin/subscriptions/{await _sub_id(session, shop)}/extend-grace",
                                 json={"days": 5}, headers=header)

    assert response.status_code == 200, response.text
    body = response.json()
    assert datetime.fromisoformat(body["grace_ends_at"]) == before + 5 * DAY
    assert body["in_grace"] is True and body["entitled"] is True
    [audit] = (await session.execute(select(AdminAuditLogs).where(AdminAuditLogs.action == "subscription.extend_grace"))).scalars()
    assert audit.details["days"] == 5
    [alert] = await _alerts(session, "plan_assigned")
    assert alert.data["action"] == "extend_grace" and alert.data["days"] == 5


async def test_extending_a_lapsed_grace_counts_from_now(client, session, shop, make_admin, mint_token):
    now = _now()
    await _paid(session, shop, status="past_due", halted_at=now - 9 * DAY, grace_ends_at=now - 2 * DAY,
                grace_reminded_at=now - 4 * DAY)
    header = await _admin_header(make_admin, mint_token)

    response = await client.post(f"{API}/admin/subscriptions/{await _sub_id(session, shop)}/extend-grace",
                                 json={"days": 3}, headers=header)

    body = response.json()
    ends = datetime.fromisoformat(body["grace_ends_at"])
    assert now + 3 * DAY <= ends <= _now() + 3 * DAY and body["entitled"] is True
    # Three days out is more than the two-day reminder: it will remind again.
    assert (await _row(session, shop["club"].id)).grace_reminded_at is None


async def test_extending_starts_a_grace_for_a_past_due_without_one(client, session, shop, make_admin, mint_token):
    await _paid(session, shop, status="past_due")
    header = await _admin_header(make_admin, mint_token)
    response = await client.post(f"{API}/admin/subscriptions/{await _sub_id(session, shop)}/extend-grace",
                                 json={"days": 10}, headers=header)
    assert response.status_code == 200, response.text
    assert response.json()["halted_at"] is not None and response.json()["in_grace"] is True


async def test_razorpay_halting_after_an_admin_started_grace_gives_the_full_seven_days(client, session, shop, make_admin, mint_token, mail):
    from app.api.subscriptions import service

    await _paid(session, shop, status="past_due")
    header = await _admin_header(make_admin, mint_token)
    await client.post(f"{API}/admin/subscriptions/{await _sub_id(session, shop)}/extend-grace", json={"days": 2}, headers=header)
    started = (await _row(session, shop["club"].id)).halted_at

    await _hook(session, "subscription.halted", "sub_paid")

    row = await _row(session, shop["club"].id)
    assert row.halted_at == started
    assert row.grace_ends_at >= _now() + timedelta(days=service.GRACE_DAYS) - timedelta(minutes=1)
    assert len(await _alerts(session, "grace_started")) == 1
    await mail.drain()
    assert len([m for m in mail.to("billing-owner@example.com") if m["subject"].startswith("Update your payment")]) == 1


@pytest.mark.parametrize("body,code", [({"days": 0}, 422), ({"days": 31}, 422), ({}, 422)])
async def test_extending_takes_one_to_thirty_days(client, session, shop, make_admin, mint_token, body, code):
    await _paid(session, shop, status="past_due")
    response = await client.post(f"{API}/admin/subscriptions/{await _sub_id(session, shop)}/extend-grace",
                                 json=body, headers=await _admin_header(make_admin, mint_token))
    assert response.status_code == code


async def test_only_a_past_due_paid_subscription_can_be_extended(client, session, shop, make_admin, mint_token):
    from app.api.subscriptions.models import ClubSubscriptions

    header = await _admin_header(make_admin, mint_token)
    await _paid(session, shop, status="active")
    sub = await _sub_id(session, shop)
    response = await client.post(f"{API}/admin/subscriptions/{sub}/extend-grace", json={"days": 3}, headers=header)
    assert response.status_code == 422 and response.json()["error_code"] == "NOT_PAST_DUE"

    row = await session.get(ClubSubscriptions, sub)
    row.status, row.activated_at = "past_due", None
    await session.commit()
    response = await client.post(f"{API}/admin/subscriptions/{sub}/extend-grace", json={"days": 3}, headers=header)
    assert response.status_code == 422 and response.json()["error_code"] == "NEVER_PAID"

    assert (await client.post(f"{API}/admin/subscriptions/999999/extend-grace", json={"days": 3}, headers=header)).status_code == 404


@pytest.mark.parametrize("role", [AdminRoles.viewer, AdminRoles.support])
async def test_extending_and_cancelling_need_the_money_role(client, session, shop, make_admin, mint_token, role):
    await _paid(session, shop, status="past_due")
    header = await _admin_header(make_admin, mint_token, role=role)
    sub = await _sub_id(session, shop)
    assert (await client.post(f"{API}/admin/subscriptions/{sub}/extend-grace", json={"days": 3}, headers=header)).status_code == 403
    assert (await client.post(f"{API}/admin/subscriptions/{sub}/cancel", json={"at_period_end": False}, headers=header)).status_code == 403


async def test_an_admin_cancelling_at_period_end(client, session, shop, razorpay, make_admin, mint_token):
    from app.api.audit.models import AdminAuditLogs

    await _paid(session, shop)
    header = await _admin_header(make_admin, mint_token)
    sub = await _sub_id(session, shop)

    response = await client.post(f"{API}/admin/subscriptions/{sub}/cancel", json={"at_period_end": True}, headers=header)

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["status"], body["cancel_at_period_end"], body["entitled"]) == ("active", True, True)
    assert razorpay.of("subscription", "cancel") == [("sub_paid", {"cancel_at_cycle_end": 1})]
    [alert] = await _alerts(session, "subscription_cancelled")
    assert alert.data["by"] == "admin" and alert.data["at_period_end"] is True
    assert await session.scalar(select(func.count(AdminAuditLogs.id)).where(AdminAuditLogs.action == "subscription.admin_cancel")) == 1

    # Razorpay's webhook at the end of the cycle tells nobody anything new.
    await _hook(session, "subscription.cancelled", "sub_paid")
    assert len(await _alerts(session, "subscription_cancelled")) == 1


async def test_an_admin_cancelling_now(client, session, shop, razorpay, make_admin, mint_token):
    from app.api.subscriptions import service

    await _paid(session, shop, status="past_due", halted_at=_now() - DAY, grace_ends_at=_now() + 6 * DAY)
    header = await _admin_header(make_admin, mint_token)

    response = await client.post(f"{API}/admin/subscriptions/{await _sub_id(session, shop)}/cancel",
                                 json={"at_period_end": False}, headers=header)

    assert response.status_code == 200, response.text
    assert (response.json()["status"], response.json()["entitled"]) == ("cancelled", False)
    assert razorpay.of("subscription", "cancel") == [("sub_paid", {"cancel_at_cycle_end": 0})]
    assert (await service.resolve_entitlements(session, shop["club"].id)).plan.key == "free"

    again = await client.post(f"{API}/admin/subscriptions/{await _sub_id(session, shop)}/cancel",
                              json={"at_period_end": False}, headers=header)
    assert again.status_code == 409 and again.json()["error_code"] == "NOTHING_TO_CANCEL"


async def test_an_admin_cancelling_a_granted_plan_touches_no_razorpay(client, session, shop, razorpay, make_admin, mint_token):
    from app.api.subscriptions import service
    from app.api.subscriptions.models import BillingCycle

    await service.assign_plan(session, club_id=shop["club"].id, plan=shop["pro"], cycle=BillingCycle.monthly)
    await session.commit()

    response = await client.post(f"{API}/admin/subscriptions/{await _sub_id(session, shop)}/cancel",
                                 json={"at_period_end": True}, headers=await _admin_header(make_admin, mint_token))

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "cancelled" and response.json()["entitled"] is False
    assert razorpay.calls == []
    assert (await service.resolve_entitlements(session, shop["club"].id)).plan.key == "free"


async def test_razorpay_refusing_an_admin_cancel_changes_nothing(client, session, shop, razorpay, make_admin, mint_token):
    def refuse(sub_id, data):
        raise RuntimeError("Razorpay down")

    razorpay.handlers[("subscription", "cancel")] = refuse
    await _paid(session, shop)

    response = await client.post(f"{API}/admin/subscriptions/{await _sub_id(session, shop)}/cancel",
                                 json={"at_period_end": False}, headers=await _admin_header(make_admin, mint_token))

    assert response.status_code == 502 and response.json()["error_code"] == "RAZORPAY_ERROR"
    row = await _row(session, shop["club"].id)
    assert row.status.value == "active" and row.cancel_at_period_end is False
    assert await _alerts(session, "subscription_cancelled") == []


async def test_the_admin_list_shows_the_grace(client, session, shop, make_admin, mint_token):
    now = _now()
    await _paid(session, shop, status="past_due", halted_at=now - DAY, grace_ends_at=now + 6 * DAY)
    response = await client.get(f"{API}/admin/subscriptions", headers=await _admin_header(make_admin, mint_token))
    [item] = response.json()["items"]
    assert item["halted_at"] is not None and item["grace_ends_at"] is not None and item["in_grace"] is True


# ─── 5. The alerts API ──────────────────────────────────────────────────


async def _three_alerts(session, shop):
    from app.api.admin_alerts import notify

    first = await notify(session, "club_applied", title="Applied", club=shop["club"])
    second = await notify(session, "payment_failed", title="Failed", body="Card declined", club=shop["club"],
                          subscription=None, data={"reason": "Card declined"}, dedupe=False)
    third = await notify(session, "payment_failed", title="Failed again", club=shop["club"], dedupe=False)
    first.created_at -= timedelta(minutes=3)
    second.created_at -= timedelta(minutes=2)
    await session.commit()
    return first, second, third


async def test_listing_notifications(client, session, shop, make_admin, mint_token):
    first, second, third = await _three_alerts(session, shop)
    header = await _admin_header(make_admin, mint_token, role=AdminRoles.viewer)

    body = (await client.get(f"{API}/admin/alerts", headers=header)).json()

    assert (body["total"], body["unread"]) == (3, 3)
    assert [i["id"] for i in body["items"]] == [third.id, second.id, first.id]
    item = body["items"][1]
    assert set(item) == {"id", "kind", "title", "body", "club", "subscription_id", "data", "created_at", "read_at"}
    assert item["club"] == {"id": shop["club"].id, "name": "Billing Club", "slug": shop["club"].slug}
    assert (item["kind"], item["body"], item["data"], item["read_at"]) == ("payment_failed", "Card declined", {"reason": "Card declined"}, None)

    by_kind = (await client.get(f"{API}/admin/alerts?kind=payment_failed&limit=1&offset=1", headers=header)).json()
    assert (by_kind["total"], [i["id"] for i in by_kind["items"]], by_kind["unread"]) == (2, [second.id], 3)
    assert (await client.get(f"{API}/admin/alerts/unread-count", headers=header)).json() == {"unread": 3}
    assert (await client.get(f"{API}/admin/alerts?limit=500", headers=header)).status_code == 422


async def test_marking_read_needs_moderation(client, session, shop, make_admin, mint_token):
    first, second, third = await _three_alerts(session, shop)
    viewer = await _admin_header(make_admin, mint_token, role=AdminRoles.viewer, email="viewer-n@example.com")
    support = await _admin_header(make_admin, mint_token, role=AdminRoles.support, email="support-n@example.com")

    assert (await client.post(f"{API}/admin/alerts/{first.id}/read", headers=viewer)).status_code == 403
    assert (await client.post(f"{API}/admin/alerts/read-all", headers=viewer)).status_code == 403

    read = await client.post(f"{API}/admin/alerts/{first.id}/read", headers=support)
    assert read.status_code == 200 and read.json()["read_at"] is not None
    stamp = read.json()["read_at"]
    assert (await client.post(f"{API}/admin/alerts/{first.id}/read", headers=support)).json()["read_at"] == stamp
    assert (await client.post(f"{API}/admin/alerts/999999/read", headers=support)).status_code == 404

    unread = (await client.get(f"{API}/admin/alerts?unread=true", headers=viewer)).json()
    assert [i["id"] for i in unread["items"]] == [third.id, second.id] and unread["total"] == 2
    everything = (await client.get(f"{API}/admin/alerts?unread=false", headers=viewer)).json()
    assert everything["total"] == 3

    done = (await client.post(f"{API}/admin/alerts/read-all", headers=support)).json()
    assert done == {"updated": 2, "unread": 0}


async def test_the_announcements_history_keeps_its_own_path(client, session, shop, make_admin, mint_token):
    # /admin/notifications was already the admin app's list of announcements
    # sent; the alerts live beside it rather than on top of it.
    await _three_alerts(session, shop)
    body = (await client.get(f"{API}/admin/notifications", headers=await _admin_header(make_admin, mint_token))).json()
    assert {"limit", "offset", "items"} <= set(body) and "unread" not in body


async def test_only_admins_see_notifications(client, session, shop, make_user, mint_token):
    person = await make_user(email="not-admin@example.com")
    assert (await client.get(f"{API}/admin/alerts", headers=auth_header(await mint_token(person)))).status_code in (401, 403)
    assert (await client.get(f"{API}/admin/alerts", headers=shop["h"])).status_code in (401, 403)
    assert (await client.get(f"{API}/admin/alerts/unread-count")).status_code == 401


# ─── 6. Totals ───────────────────────────────────────────────────────────


async def test_totals_count_pending_paused_and_in_grace(client, session, make_club, make_admin, mint_token):
    from app.api.subscriptions.models import ClubSubscriptions

    plan = await make_plan(session, key="totals_plan", monthly=10000)
    now = _now()
    rows = [
        dict(status="pending", pending_since=now),
        dict(status="paused"),
        dict(status="past_due", activated_at=now - 40 * DAY, halted_at=now - DAY, grace_ends_at=now + 6 * DAY),
        dict(status="past_due", activated_at=now - 40 * DAY, halted_at=now - 9 * DAY, grace_ends_at=now - 2 * DAY),
        dict(status="past_due", activated_at=now - 40 * DAY),
        dict(status="active", activated_at=now - 40 * DAY, mrr_paise=10000),
    ]
    for i, extra in enumerate(rows):
        _, club = await make_club(name=f"Totals {i}", email=f"totals{i}@example.com")
        session.add(ClubSubscriptions(club_id=club.id, plan_id=plan.id, razorpay_subscription_id=f"sub_t{i}", **extra))
    await session.commit()

    totals = (await client.get(f"{API}/admin/subscriptions/totals", headers=await _admin_header(make_admin, mint_token))).json()

    assert (totals["pending"], totals["paused"], totals["in_grace"], totals["past_due"]) == (1, 1, 1, 3)
    assert totals["mrr_paise"] >= 10000
