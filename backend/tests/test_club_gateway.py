"""A club taking its ticket and merch payments into its own Razorpay account.

Razorpay is live in production; nothing here reaches it. Every account --
the platform's and each club's -- is a FakeAccount, and the real client's
request method is made to fail the test if anything gets past the fakes.
"""
import hashlib
import hmac
import itertools
import json
import logging
import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from razorpay.errors import BadRequestError
from sqlalchemy import select

from app.api.admin_alerts.models import AdminNotifications
from app.api.audit.models import AdminAuditLogs
from app.api.club_access import policy
from app.api.club_access.models import ClubMembers
from app.api.events.models import EventRegistrationsLink
from app.api.payment_gateways import crypto
from app.api.payment_gateways import service as gateways
from app.api.payment_gateways.models import ClubPaymentGateways
from app.api.payments import service as payments
from app.api.payments.models import OrderStatus, PaymentLogs, PaymentOrders, PaymentStatus, RazorpayWebhookLogs
from app.api.store.models import EventStoreOrders
from app.api.users.models import AdminRoles
from app.config import settings
from app.core import public_urls, redact
from app.core.auth.jwt import create_access_token
from app.api.auth.schemas import AuthTokenData
from tests.conftest import auth_header

API = "/api/v1"
GATEWAY = f"{API}/clubs/payments/gateway"
_n = itertools.count(1)

CLUB_KEY = "rzp_live_ClubAAAA111111"
CLUB_SECRET = "clubSecretAAAA1111111111"
OTHER_KEY = "rzp_live_ClubBBBB222222"
OTHER_SECRET = "clubSecretBBBB2222222222"


class FakeAccount:
    """One Razorpay account: the orders made on it, the payments in it, and
    every call that reached it."""

    def __init__(self, key_id: str, secret: str):
        self.key_id, self.secret = key_id, secret
        self.valid = True
        self.down = False
        self.payments: dict[str, dict] = {}
        self.calls: list[tuple] = []
        self.order = SimpleNamespace(create=self._order_create, all=self._order_all)
        self.payment = SimpleNamespace(fetch=self._payment_fetch, refund=self._payment_refund)

    def _gate(self):
        if self.down:
            raise ConnectionError("Razorpay is down")
        if not self.valid:
            raise BadRequestError("Authentication failed")

    def _order_create(self, data=None, **kwargs):
        self._gate()
        order_id = f"order_{self.key_id[-6:]}_{next(_n)}"
        self.calls.append(("order.create", order_id, data["amount"]))
        return {"id": order_id, "amount": data["amount"], "status": "created"}

    def _order_all(self, data=None, **kwargs):
        self._gate()
        self.calls.append(("order.all",))
        return {"entity": "collection", "count": 0, "items": []}

    def _payment_fetch(self, payment_id, data=None, **kwargs):
        self._gate()
        self.calls.append(("payment.fetch", payment_id))
        if payment_id not in self.payments:
            raise BadRequestError("The id provided does not exist")
        return dict(self.payments[payment_id])

    def _payment_refund(self, payment_id, data=None, **kwargs):
        self._gate()
        self.calls.append(("payment.refund", payment_id))
        payment = self.payments[payment_id]
        payment["status"], payment["amount_refunded"] = "refunded", payment["amount"]
        return {"id": f"rfnd_{next(_n)}", "payment_id": payment_id}

    def pay(self, order_id: str, rupees, *, status="captured") -> str:
        """A payment the payer made at checkout, as Razorpay holds it."""
        payment_id = f"pay_{self.key_id[-6:]}_{next(_n)}"
        self.payments[payment_id] = {
            "id": payment_id, "order_id": order_id, "status": status, "method": "upi",
            "amount": int(Decimal(str(rupees)) * 100), "amount_refunded": 0, "upi": {"vpa": "payer@upi"},
        }
        return payment_id

    def fetched(self) -> list:
        return [c for c in self.calls if c[0] == "payment.fetch"]


@pytest.fixture
def razorpay_world(monkeypatch):
    """The platform's account and a ledger of club accounts. A club client
    built with the wrong secret answers like Razorpay: authentication failed."""

    def unreachable(*args, **kwargs):
        raise AssertionError("a test tried to reach Razorpay")

    import razorpay.client

    monkeypatch.setattr(razorpay.client.Client, "request", unreachable)
    monkeypatch.setattr(settings, "PAYMENT_KEYS_SECRET", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "ALLOW_TEST_GATEWAY_KEYS", False)

    platform = FakeAccount(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET)
    monkeypatch.setattr(payments, "razorpay_client", platform)
    accounts = {key: FakeAccount(key, secret) for key, secret in ((CLUB_KEY, CLUB_SECRET), (OTHER_KEY, OTHER_SECRET))}

    def make_client(key_id, key_secret):
        account = accounts.get(key_id)
        if account is None or account.secret != key_secret:
            stranger = FakeAccount(key_id, key_secret)
            stranger.valid = False
            return stranger
        return account

    monkeypatch.setattr(gateways, "make_client", make_client)
    # The payer's receipt is a PDF; not what these tests are about.
    monkeypatch.setattr(payments, "send_payment_confirmation_email", lambda **kwargs: None)
    return SimpleNamespace(platform=platform, accounts=accounts)


@pytest.fixture
async def world(client, session, make_club, make_user, make_event, make_admin, mint_token, razorpay_world):
    owner, club = await make_club(name=f"Circuit Society {next(_n)}", email=f"circuit{next(_n)}@example.com")
    rival_owner, rival = await make_club(name=f"Robotics Guild {next(_n)}", email=f"robotics{next(_n)}@example.com")
    event = await make_event(club=club, created_by=owner, name="Hack Night", has_fee=True, reg_fee=100.0)
    rival_event = await make_event(club=rival, created_by=rival_owner, name="Bot Brawl", has_fee=True, reg_fee=100.0)
    admin = await make_admin(email=f"money{next(_n)}@example.com")
    await session.commit()
    return SimpleNamespace(
        owner=owner, club=club, rival=rival, event=event, rival_event=rival_event,
        h=auth_header(await mint_token(owner)), rival_h=auth_header(await mint_token(rival_owner)),
        admin=admin, admin_h=auth_header(await mint_token(admin)),
        rz=razorpay_world, platform=razorpay_world.platform,
        account=razorpay_world.accounts[CLUB_KEY], other=razorpay_world.accounts[OTHER_KEY],
    )


async def _connect(client, h, key=CLUB_KEY, secret=CLUB_SECRET):
    r = await client.put(GATEWAY, json={"key_id": key, "key_secret": secret}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


async def _payer(session, make_user, make_registration, event, *, amount=100.0, **kw):
    payer = await make_user(email=f"payer{next(_n)}@example.com")
    reg = await make_registration(event=event, user=payer, actual_amount=amount, paid_amount=0.0, **kw)
    await session.commit()
    return payer, reg


async def _order(client, mint_token, payer, event, reg, *, checkout=2):
    params = {"checkout": checkout} if checkout is not None else {}
    return await client.post(
        f"{API}/payments/orders",
        params=params,
        json={"source": "event_registration", "payload": {"event_id": event.id, "event_registration_id": str(reg.id)}},
        headers=auth_header(await mint_token(payer)),
    )


def _signed(secret: str, body: dict) -> tuple[bytes, str]:
    raw = json.dumps(body).encode()
    return raw, hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


async def _hook(client, gateway_out: dict, body: dict, *, event_id=None, secret=None):
    raw, signature = _signed(secret or gateway_out["webhook_secret"], body)
    path = gateway_out["webhook_url"].split("://", 1)[1].split("/", 1)[1]
    return await client.post(
        f"/{path}", content=raw,
        headers={"X-Razorpay-Signature": signature, "X-Razorpay-Event-Id": event_id or f"evt_{next(_n)}",
                 "Content-Type": "application/json"},
    )


def _payment_event(name, payment_id, order_id, *, status="captured", amount=10000):
    return {"entity": "event", "event": name, "payload": {"payment": {"entity": {
        "id": payment_id, "order_id": order_id, "status": status, "method": "upi", "amount": amount, "upi": {},
    }}}}


async def _alerts(session, kind):
    return list((await session.scalars(select(AdminNotifications).where(AdminNotifications.kind == kind))).all())


# ─── Encryption and availability ────────────────────────────────────────


async def test_secrets_round_trip_through_fernet_and_survive_a_key_rotation(session, monkeypatch):
    old = Fernet.generate_key().decode()
    monkeypatch.setattr(settings, "PAYMENT_KEYS_SECRET", old)
    token = crypto.encrypt(CLUB_SECRET)
    assert CLUB_SECRET not in token and crypto.decrypt(token) == CLUB_SECRET

    # Rotated: the new key encrypts, the old one still reads what it wrote.
    new = Fernet.generate_key().decode()
    monkeypatch.setattr(settings, "PAYMENT_KEYS_SECRET", f"{new},{old}")
    assert crypto.decrypt(token) == CLUB_SECRET
    monkeypatch.setattr(settings, "PAYMENT_KEYS_SECRET", new)
    with pytest.raises(crypto.Unavailable):
        crypto.decrypt(token)

    for unusable in ("", "not-a-fernet-key"):
        monkeypatch.setattr(settings, "PAYMENT_KEYS_SECRET", unusable)
        assert crypto.available() is False
        with pytest.raises(crypto.Unavailable):
            crypto.encrypt("x")


async def test_without_payment_keys_secret_the_feature_is_unavailable(client, session, world, monkeypatch):
    monkeypatch.setattr(settings, "PAYMENT_KEYS_SECRET", "")
    body = (await client.get(GATEWAY, headers=world.h)).json()
    assert body["available"] is False and body["can_manage"] is False and body["connected"] is False
    assert body["webhook_events"] == gateways.WEBHOOK_EVENTS

    r = await client.put(GATEWAY, json={"key_id": CLUB_KEY, "key_secret": CLUB_SECRET}, headers=world.h)
    assert r.status_code == 503 and r.json()["error_code"] == "GATEWAY_UNAVAILABLE"
    assert world.account.calls == []
    assert await session.scalar(select(ClubPaymentGateways.id).where(ClubPaymentGateways.club_id == world.club.id)) is None


# ─── Connecting ─────────────────────────────────────────────────────────


async def test_the_owner_connects_and_the_secret_is_never_shown_logged_or_audited(client, session, world, caplog):
    caplog.set_level(logging.DEBUG)
    out = await _connect(client, world.h)

    assert out["available"] is True and out["connected"] is True and out["can_manage"] is True
    assert out["status"] == "active" and out["mode"] == "keys" and out["key_id"] == CLUB_KEY
    assert out["verified_at"] and out["last_webhook_at"] is None and out["last_error"] is None
    assert out["webhook_url"].startswith(f"{settings.PUBLIC_API_URL}/api/v1/payments/webhook/club/")
    token = out["webhook_url"].rsplit("/", 1)[1]
    assert len(token) >= 32 and len(out["webhook_secret"]) >= 43
    assert out["webhook_events"] == ["payment.authorized", "payment.captured", "payment.failed", "order.paid", "refund.processed"]
    # The keys were checked with a harmless read on the club's own account.
    assert world.account.calls == [("order.all",)]

    row = await session.scalar(select(ClubPaymentGateways).where(ClubPaymentGateways.club_id == world.club.id))
    assert row.created_by == world.owner.id and row.webhook_token == token
    assert CLUB_SECRET not in row.key_secret_enc and crypto.decrypt(row.key_secret_enc) == CLUB_SECRET
    assert out["webhook_secret"] not in row.webhook_secret_enc

    # Reading it back, and every place the change was written down.
    again = await client.get(GATEWAY, headers=world.h)
    audit = list((await session.scalars(select(AdminAuditLogs).where(AdminAuditLogs.entity_id == str(world.club.id)))).all())
    assert [a.action for a in audit] == ["payment_gateway.connect"] and audit[0].details["key_id"] == CLUB_KEY
    connected = await _alerts(session, "payment_gateway_connected")
    assert len(connected) == 1 and connected[0].club_id == world.club.id and connected[0].data["by"] == "club"
    listed = await client.get(f"{API}/admin/payment-gateways", headers=world.admin_h)
    for text in (
        json.dumps(out), again.text, listed.text, caplog.text,
        json.dumps([[a.summary, a.details] for a in audit]),
        json.dumps([[n.title, n.body, n.data] for n in connected]),
    ):
        assert CLUB_SECRET not in text
    assert out["webhook_secret"] not in listed.text and out["webhook_secret"] not in caplog.text


async def test_keys_razorpay_refuses_test_keys_and_malformed_keys_are_refused(client, session, world, monkeypatch):
    wrong = "wrongSecretZZZZ999999999"
    r = await client.put(GATEWAY, json={"key_id": CLUB_KEY, "key_secret": wrong}, headers=world.h)
    assert r.status_code == 422 and r.json()["error_code"] == "INVALID_KEYS"
    assert r.json()["errors"]["reason"] == "Authentication failed" and wrong not in r.text

    for key, secret in (("pk_live_nope", CLUB_SECRET), (CLUB_KEY, "has spaces in it"), (CLUB_KEY, "short")):
        r = await client.put(GATEWAY, json={"key_id": key, "key_secret": secret}, headers=world.h)
        assert r.status_code == 422 and r.json()["error_code"] == "INVALID_KEYS", (key, r.text)
        assert secret not in r.text
    r = await client.put(GATEWAY, json={"key_id": CLUB_KEY, "key_secret": "x" * 500}, headers=world.h)
    assert r.status_code == 422 and "x" * 129 not in r.text
    # The platform's own keys are not a club's.
    r = await client.put(GATEWAY, json={"key_id": "rzp_live_PlatformKey1", "key_secret": CLUB_SECRET}, headers=world.h)
    assert r.status_code == 422
    monkeypatch.setattr(settings, "RAZORPAY_KEY_ID", "rzp_live_PlatformKey1")
    r = await client.put(GATEWAY, json={"key_id": "rzp_live_PlatformKey1", "key_secret": CLUB_SECRET}, headers=world.h)
    assert r.status_code == 422 and r.json()["errors"]["reason"] == "platform_keys"

    test_key, test_secret = "rzp_test_ClubTTTT333333", "testSecretTTTT3333333333"
    world.rz.accounts[test_key] = FakeAccount(test_key, test_secret)
    r = await client.put(GATEWAY, json={"key_id": test_key, "key_secret": test_secret}, headers=world.h)
    assert r.status_code == 422 and r.json()["error_code"] == "TEST_KEYS_NOT_ALLOWED"
    assert world.rz.accounts[test_key].calls == []

    # Razorpay itself unreachable: nothing saved, and not blamed on the keys.
    world.account.down = True
    r = await client.put(GATEWAY, json={"key_id": CLUB_KEY, "key_secret": CLUB_SECRET}, headers=world.h)
    assert r.status_code == 502 and r.json()["error_code"] == "RAZORPAY_UNREACHABLE"
    assert await session.scalar(select(ClubPaymentGateways.id).where(ClubPaymentGateways.club_id == world.club.id)) is None

    monkeypatch.setattr(settings, "ALLOW_TEST_GATEWAY_KEYS", True)
    assert (await _connect(client, world.h, test_key, test_secret))["key_id"] == test_key


async def test_only_the_owner_changes_it_and_only_money_admins_read_it(client, session, world):
    owner_view = await _connect(client, world.h)
    world.account.calls.clear()
    for role in ("admin", "editor", "door"):
        member = ClubMembers(club_id=world.club.id, email=f"{role}{next(_n)}@example.com", role=role, status="active")
        session.add(member)
        await session.commit()
        await session.refresh(world.owner)
        token = create_access_token(data=AuthTokenData(
            user_id=world.owner.id, token_type="access_token", token_version=world.owner.token_version, member_id=member.id,
        ).model_dump())
        h = auth_header(token)
        for method, path in (("PUT", GATEWAY), ("POST", f"{GATEWAY}/verify"), ("DELETE", GATEWAY)):
            kwargs = {"json": {"key_id": OTHER_KEY, "key_secret": OTHER_SECRET}} if method == "PUT" else {}
            r = await client.request(method, path, headers=h, **kwargs)
            assert r.status_code == 403 and r.json()["error_code"] == "OWNER_ONLY", (role, method, r.text)
        seen = await client.get(GATEWAY, headers=h)
        if role == "admin":
            # Like the club's finance: an admin member reads it, without the webhook secret.
            body = seen.json()
            assert seen.status_code == 200 and body["webhook_secret"] is None and body["can_manage"] is False
            assert body["key_id"] == CLUB_KEY and body["status"] == "active" and body["webhook_url"] == owner_view["webhook_url"]
            assert owner_view["webhook_secret"] not in seen.text
        else:
            assert seen.status_code == 403 and seen.json()["error_code"] == "ROLE_FORBIDDEN"
        assert policy.allows(role, "GET", GATEWAY) is (role == "admin")
        assert policy.allows(role, "PUT", GATEWAY) is False and policy.can(role)["payment_gateway"] is False
    assert policy.can("owner")["payment_gateway"] is True
    assert world.account.calls == [] and world.other.calls == []
    row = await session.scalar(select(ClubPaymentGateways).where(ClubPaymentGateways.club_id == world.club.id))
    assert row.status == "active" and row.key_id == CLUB_KEY


# ─── Orders: which account, and what checkout opens with ────────────────


async def test_each_order_is_made_on_the_account_of_the_club_selling_it(client, session, world, make_user, make_registration, make_event):
    await _connect(client, world.h)
    world.account.calls.clear()

    # A ticket for the club with its own account: made there, opened with its key.
    payer, reg = await _payer(session, make_user, make_registration, world.event)
    r = await _order(client, _mint(world), payer, world.event, reg)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["gateway"] == "club" and body["key_id"] == CLUB_KEY and float(body["amount"]) == 100.0
    assert world.account.calls == [("order.create", body["razorpay_order_id"], 10000)] and world.platform.calls == []
    row = await session.scalar(select(PaymentOrders).where(PaymentOrders.razorpay_order_id == body["razorpay_order_id"]))
    assert row.payment_gateway_id is not None

    # Asked again for the same money: the same order, still saying where it lives.
    again = (await _order(client, _mint(world), payer, world.event, reg)).json()
    assert again["id"] == body["id"] and again["gateway"] == "club" and again["key_id"] == CLUB_KEY

    # The store's picks ride on the ticket's payment, so they go to the club too.
    shopper, shop_reg = await _payer(session, make_user, make_registration, world.event)
    session.add(EventStoreOrders(event_id=world.event.id, registration_id=shop_reg.id, user_id=shopper.id, status="pending", total=Decimal("450")))
    await session.commit()
    shop = (await _order(client, _mint(world), shopper, world.event, shop_reg)).json()
    assert shop["gateway"] == "club" and float(shop["amount"]) == 550.0 and shop["key_id"] == CLUB_KEY
    # Merch alone, the ticket already paid: still the club's.
    late, late_reg = await _payer(session, make_user, make_registration, world.event, is_paid=True)
    late_reg.paid_amount = 100
    session.add(EventStoreOrders(event_id=world.event.id, registration_id=late_reg.id, user_id=late.id, status="pending", total=Decimal("200")))
    await session.commit()
    merch = (await _order(client, _mint(world), late, world.event, late_reg)).json()
    assert merch["gateway"] == "club" and float(merch["amount"]) == 200.0

    # Another club, without its own account: the platform's, as today.
    rival_payer, rival_reg = await _payer(session, make_user, make_registration, world.rival_event)
    rival = (await _order(client, _mint(world), rival_payer, world.rival_event, rival_reg)).json()
    assert rival["gateway"] == "platform" and rival["key_id"] == settings.RAZORPAY_KEY_ID
    assert [c[0] for c in world.platform.calls] == ["order.create"]
    rival_row = await session.scalar(select(PaymentOrders).where(PaymentOrders.razorpay_order_id == rival["razorpay_order_id"]))
    assert rival_row.payment_gateway_id is None

    # An event no club runs: the platform's.
    orphan_event = await make_event(club=None, created_by=world.owner, name="Platform meetup", has_fee=True, reg_fee=50.0)
    orphan_payer, orphan_reg = await _payer(session, make_user, make_registration, orphan_event, amount=50.0)
    assert (await _order(client, _mint(world), orphan_payer, orphan_event, orphan_reg)).json()["gateway"] == "platform"

    # The registration's own event decides, not the event_id the caller wrote.
    sneaky = await client.post(
        f"{API}/payments/orders", params={"checkout": 2},
        json={"source": "event_registration", "payload": {"event_id": world.rival_event.id, "event_registration_id": str(reg.id)}},
        headers=auth_header(await _mint(world)(payer)),
    )
    assert sneaky.json()["gateway"] == "club"


def _mint(world):
    """A token for any user, without the fixture plumbing."""

    async def mint(user):
        return create_access_token(data=AuthTokenData(
            user_id=user.id, token_type="access_token", token_version=user.token_version,
        ).model_dump())

    return mint


async def test_old_app_builds_are_told_to_update_for_a_club_account(client, session, world, make_user, make_registration):
    # Platform events: exactly as today, flag or not.
    rival_payer, rival_reg = await _payer(session, make_user, make_registration, world.rival_event)
    plain = await _order(client, _mint(world), rival_payer, world.rival_event, rival_reg, checkout=None)
    assert plain.status_code == 200 and plain.json()["gateway"] == "platform"
    assert (await _order(client, _mint(world), rival_payer, world.rival_event, rival_reg)).json()["id"] == plain.json()["id"]

    await _connect(client, world.h)
    payer, reg = await _payer(session, make_user, make_registration, world.event)
    for checkout in (None, 1):
        r = await _order(client, _mint(world), payer, world.event, reg, checkout=checkout)
        assert r.status_code == 409, r.text
        assert r.json()["error_code"] == "UPDATE_APP_TO_PAY"
        assert r.json()["message"] == "Update the MyOtherApp app to pay for this event, or pay on the website."
        assert r.json()["errors"]["web_url"] == public_urls.event_url(world.event.slug)
    assert [c for c in world.account.calls if c[0] == "order.create"] == []
    assert await session.scalar(select(PaymentOrders.id).where(PaymentOrders.receipt == f"er_{reg.ticket_id}")) is None
    # The new build pays it.
    assert (await _order(client, _mint(world), payer, world.event, reg)).json()["gateway"] == "club"


async def test_keys_refused_while_making_an_order_fail_over_to_the_platform(client, session, world, make_user, make_registration):
    await _connect(client, world.h)
    world.account.valid = False  # revoked in Razorpay's dashboard
    payer, reg = await _payer(session, make_user, make_registration, world.event)
    r = await _order(client, _mint(world), payer, world.event, reg)
    assert r.status_code == 200 and r.json()["gateway"] == "platform" and r.json()["key_id"] == settings.RAZORPAY_KEY_ID
    row = await session.scalar(select(ClubPaymentGateways).where(ClubPaymentGateways.club_id == world.club.id))
    await session.refresh(row)
    assert row.status == "failed" and "Authentication failed" in row.last_error
    assert len(await _alerts(session, "payment_gateway_failed")) == 1
    info = (await client.get(f"{API}/events/info/{world.event.id}")).json()
    assert info["payments_to"] == "platform"


# ─── Verify and refund: the order's own account ─────────────────────────


async def test_verify_and_refund_use_the_orders_own_account_after_the_club_changes_it(
    client, session, world, make_user, make_registration
):
    await _connect(client, world.h)
    payer, reg = await _payer(session, make_user, make_registration, world.event)
    first = (await _order(client, _mint(world), payer, world.event, reg)).json()
    payer2, reg2 = await _payer(session, make_user, make_registration, world.event)
    second = (await _order(client, _mint(world), payer2, world.event, reg2)).json()
    assert first["key_id"] == second["key_id"] == CLUB_KEY

    # The club moves to another account, then drops its own altogether.
    await _connect(client, world.h, OTHER_KEY, OTHER_SECRET)
    assert (await client.delete(GATEWAY, headers=world.h)).json()["connected"] is False
    rows = list((await session.scalars(select(ClubPaymentGateways).where(ClubPaymentGateways.club_id == world.club.id).order_by(ClubPaymentGateways.id))).all())
    assert [r.status for r in rows] == ["replaced", "disconnected"]

    # The first order's payment is still checked in the first account...
    payment_id = world.account.pay(first["razorpay_order_id"], 100)
    r = await client.post(f"{API}/payments/verify", json={"razorpay_order_id": first["razorpay_order_id"], "razorpay_payment_id": payment_id})
    assert r.status_code == 200 and r.json()["payment_status"] == "captured", r.text
    assert world.account.fetched() == [("payment.fetch", payment_id)] and world.other.fetched() == [] and world.platform.fetched() == []
    await session.refresh(reg)
    assert reg.is_paid is True

    # ...and refunded through it.
    log = await session.scalar(select(PaymentLogs).where(PaymentLogs.razorpay_payment_id == payment_id))
    refund = await client.post(f"{API}/payments/logs/{log.id}/refund", headers=world.admin_h)
    assert refund.status_code == 200, refund.text
    assert ("payment.refund", payment_id) in world.account.calls
    assert not [c for c in world.platform.calls + world.other.calls if c[0] == "payment.refund"]
    await session.refresh(reg)
    assert reg.is_paid is False

    # New orders now go to the platform.
    payer3, reg3 = await _payer(session, make_user, make_registration, world.event)
    assert (await _order(client, _mint(world), payer3, world.event, reg3, checkout=None)).json()["gateway"] == "platform"


async def test_an_order_whose_keys_cannot_be_read_is_not_verified_elsewhere(client, session, world, make_user, make_registration, monkeypatch):
    await _connect(client, world.h)
    payer, reg = await _payer(session, make_user, make_registration, world.event)
    order = (await _order(client, _mint(world), payer, world.event, reg)).json()
    payment_id = world.account.pay(order["razorpay_order_id"], 100)
    monkeypatch.setattr(settings, "PAYMENT_KEYS_SECRET", Fernet.generate_key().decode())
    r = await client.post(f"{API}/payments/verify", json={"razorpay_order_id": order["razorpay_order_id"], "razorpay_payment_id": payment_id})
    assert r.status_code == 503 and r.json()["error_code"] == "GATEWAY_UNAVAILABLE"
    assert world.platform.fetched() == []


# ─── The club's webhook ─────────────────────────────────────────────────


async def test_club_webhook_signature_token_and_idempotency(client, session, world, make_user, make_registration):
    out = await _connect(client, world.h)
    payer, reg = await _payer(session, make_user, make_registration, world.event)
    order = (await _order(client, _mint(world), payer, world.event, reg)).json()
    payment_id = world.account.pay(order["razorpay_order_id"], 100)
    event = _payment_event("payment.captured", payment_id, order["razorpay_order_id"])

    # Unknown and malformed tokens: nothing there.
    for token in ("A" * 32, "short", "../../etc"):
        r = await client.post(f"{API}/payments/webhook/club/{token}", content=b"{}", headers={"X-Razorpay-Signature": "x"})
        assert r.status_code == 404

    # A bad signature: refused, said on the club's page, the admins told once.
    for _ in range(2):
        r = await _hook(client, out, event, secret="not-the-secret")
        assert r.status_code == 400
    assert await session.scalar(select(RazorpayWebhookLogs.id).where(RazorpayWebhookLogs.event_id.like("gw%"))) is None
    assert len(await _alerts(session, "payment_gateway_failed")) == 1
    page = (await client.get(GATEWAY, headers=world.h)).json()
    assert "signature" in page["last_error"] and page["status"] == "active"
    await session.refresh(reg)
    assert reg.is_paid is False

    # The real thing, twice: applied once, recorded once.
    for _ in range(2):
        r = await _hook(client, out, event, event_id="evt_capture_1")
        assert r.status_code == 200, r.text
    assert r.json() == {"status": "duplicate"}
    await session.refresh(reg)
    assert reg.is_paid is True
    gateway_id = (await session.scalar(select(ClubPaymentGateways).where(ClubPaymentGateways.club_id == world.club.id))).id
    logs = list((await session.scalars(select(RazorpayWebhookLogs).where(RazorpayWebhookLogs.event_id == f"gw{gateway_id}:evt_capture_1"))).all())
    assert len(logs) == 1
    assert len(list((await session.scalars(select(PaymentLogs).where(PaymentLogs.razorpay_payment_id == payment_id))).all())) == 1
    page = (await client.get(GATEWAY, headers=world.h)).json()
    assert page["last_webhook_at"] is not None and page["last_error"] is None

    # A subscription event on the club's account is recorded and ignored.
    r = await _hook(client, out, {"entity": "event", "event": "subscription.charged", "payload": {}})
    assert r.status_code == 200 and r.json() == {"status": "ignored"}


async def test_a_club_webhook_is_only_a_hint(client, session, world, make_user, make_registration):
    """A forged event, signed with a leaked webhook secret, mints nothing:
    Razorpay is asked, with the club's keys, and what it says is applied."""
    out = await _connect(client, world.h)
    payer, reg = await _payer(session, make_user, make_registration, world.event)
    order = (await _order(client, _mint(world), payer, world.event, reg)).json()

    # Razorpay holds the payment as authorized only (or not at all).
    pending = world.account.pay(order["razorpay_order_id"], 100, status="authorized")
    r = await _hook(client, out, _payment_event("payment.captured", pending, order["razorpay_order_id"]))
    assert r.status_code == 200 and r.json()["status"] == "authorized"
    assert ("payment.fetch", pending) in world.account.calls
    await session.refresh(reg)
    assert reg.is_paid is False
    r = await _hook(client, out, _payment_event("order.paid", "pay_made_up", order["razorpay_order_id"]))
    assert r.status_code == 200 and r.json()["status"] == "ignored"
    await session.refresh(reg)
    assert reg.is_paid is False

    # A payment Razorpay says belongs to another order is applied to that one only.
    rival_payer, rival_reg = await _payer(session, make_user, make_registration, world.rival_event)
    platform_order = (await _order(client, _mint(world), rival_payer, world.rival_event, rival_reg)).json()
    elsewhere = world.account.pay(platform_order["razorpay_order_id"], 100)
    r = await _hook(client, out, _payment_event("payment.captured", elsewhere, platform_order["razorpay_order_id"]))
    assert r.json()["status"] == "ignored"
    await session.refresh(rival_reg)
    assert rival_reg.is_paid is False

    # Razorpay unreachable: answered 502, and the retry is not taken for a duplicate.
    real = world.account.pay(order["razorpay_order_id"], 100)
    world.account.down = True
    r = await _hook(client, out, _payment_event("payment.captured", real, order["razorpay_order_id"]), event_id="evt_retry")
    assert r.status_code == 502
    world.account.down = False
    r = await _hook(client, out, _payment_event("payment.captured", real, order["razorpay_order_id"]), event_id="evt_retry")
    assert r.status_code == 200 and r.json()["status"] == "captured"
    await session.refresh(reg)
    assert reg.is_paid is True

    # A refund claimed but not made: nothing. Made, in full: the ticket is unpaid again.
    refund_event = {"entity": "event", "event": "refund.processed", "payload": {"refund": {"entity": {"id": "rfnd_1", "payment_id": real}}}}
    r = await _hook(client, out, refund_event)
    assert r.json()["status"] == "ignored"
    await session.refresh(reg)
    assert reg.is_paid is True
    world.account.payments[real].update(status="refunded", amount_refunded=10000)
    r = await _hook(client, out, refund_event)
    assert r.json()["status"] == "refunded"
    await session.refresh(reg)
    assert reg.is_paid is False


async def test_the_platform_webhook_cannot_move_an_order_on_a_clubs_account(client, session, world, make_user, make_registration):
    await _connect(client, world.h)
    payer, reg = await _payer(session, make_user, make_registration, world.event)
    order = (await _order(client, _mint(world), payer, world.event, reg)).json()
    await payments.handle_razorpay_webhook(
        session, _payment_event("payment.captured", "pay_platform_claim", order["razorpay_order_id"]),
        event_id=f"evt_platform_{next(_n)}", signature="sig",
    )
    await session.refresh(reg)
    assert reg.is_paid is False
    assert await session.scalar(select(PaymentLogs.id).where(PaymentLogs.razorpay_payment_id == "pay_platform_claim")) is None


# ─── Checking again, disconnecting ──────────────────────────────────────


async def test_verify_marks_failed_keys_and_recovers(client, session, world, make_user, make_registration):
    r = await client.post(f"{GATEWAY}/verify", headers=world.h)
    assert r.status_code == 404 and r.json()["error_code"] == "NO_GATEWAY"
    await _connect(client, world.h)
    assert (await client.get(f"{API}/events/info/{world.event.id}")).json()["payments_to"] == "club"

    world.account.valid = False
    out = (await client.post(f"{GATEWAY}/verify", headers=world.h)).json()
    assert out["status"] == "failed" and out["connected"] is True and "Authentication failed" in out["last_error"]
    assert len(await _alerts(session, "payment_gateway_failed")) == 1
    assert (await client.get(f"{API}/events/info/{world.event.id}")).json()["payments_to"] == "platform"
    payer, reg = await _payer(session, make_user, make_registration, world.event)
    assert (await _order(client, _mint(world), payer, world.event, reg, checkout=None)).json()["gateway"] == "platform"

    # Razorpay down is not the keys' fault: the status stays.
    world.account.valid, world.account.down = True, True
    out = (await client.post(f"{GATEWAY}/verify", headers=world.h)).json()
    assert out["status"] == "failed" and "reach Razorpay" in out["last_error"]

    world.account.down = False
    out = (await client.post(f"{GATEWAY}/verify", headers=world.h)).json()
    assert out["status"] == "active" and out["last_error"] is None
    assert (await client.get(f"{API}/events/info/{world.event.id}")).json()["payments_to"] == "club"
    actions = list((await session.scalars(select(AdminAuditLogs.action).where(AdminAuditLogs.entity_id == str(world.club.id)))).all())
    assert actions.count("payment_gateway.verify") == 3


async def test_disconnect_falls_back_to_the_platform_and_says_so(client, session, world):
    await _connect(client, world.h)
    out = (await client.delete(GATEWAY, headers=world.h)).json()
    assert out["connected"] is False and out["status"] == "disconnected" and out["key_id"] == CLUB_KEY
    assert out["webhook_url"] is None and out["webhook_secret"] is None and out["can_manage"] is True
    disconnected = await _alerts(session, "payment_gateway_disconnected")
    assert len(disconnected) == 1 and disconnected[0].data["by"] == "club"
    assert (await client.get(f"{API}/events/info/{world.event.id}")).json()["payments_to"] == "platform"
    # Again: nothing to do, the same answer.
    assert (await client.delete(GATEWAY, headers=world.h)).json()["status"] == "disconnected"
    assert len(await _alerts(session, "payment_gateway_disconnected")) == 1


# ─── Admin ──────────────────────────────────────────────────────────────


async def test_admins_list_and_disable_club_gateways(client, session, world, make_admin, mint_token, make_user, make_registration):
    await _connect(client, world.h)
    await _connect(client, world.h, OTHER_KEY, OTHER_SECRET)  # the first is replaced
    await _connect(client, world.rival_h)
    world.account.valid = False
    await client.post(f"{GATEWAY}/verify", headers=world.rival_h)  # the rival's fails

    listed = (await client.get(f"{API}/admin/payment-gateways", headers=world.admin_h)).json()
    assert listed["total"] == 2
    by_club = {item["club"]["id"]: item for item in listed["items"]}
    assert by_club[world.club.id]["status"] == "active" and by_club[world.club.id]["key_id"] == OTHER_KEY
    assert by_club[world.club.id]["club"] == {"id": world.club.id, "name": world.club.name, "slug": world.club.slug}
    assert by_club[world.rival.id]["status"] == "failed" and by_club[world.rival.id]["last_error"]
    for key in ("verified_at", "last_webhook_at", "created_at"):
        assert key in by_club[world.club.id]
    assert (await client.get(f"{API}/admin/payment-gateways", params={"status": "replaced"}, headers=world.admin_h)).json()["total"] == 1
    found = (await client.get(f"{API}/admin/payment-gateways", params={"q": world.rival.name[:10]}, headers=world.admin_h)).json()
    assert [i["club"]["id"] for i in found["items"]] == [world.rival.id]
    assert (await client.get(f"{API}/admin/payment-gateways", params={"limit": 1}, headers=world.admin_h)).json()["total"] == 2
    assert (await client.get(f"{API}/admin/payment-gateways", params={"status": "nope"}, headers=world.admin_h)).status_code == 422
    assert (await client.get(f"{API}/admin/payment-gateways", headers=world.h)).status_code == 403

    # Disabling is money: a viewer cannot.
    viewer = await make_admin(email=f"viewer{next(_n)}@example.com", role=AdminRoles.viewer)
    r = await client.post(f"{API}/admin/payment-gateways/{world.club.id}/disable", headers=auth_header(await mint_token(viewer)))
    assert r.status_code == 403
    r = await client.post(f"{API}/admin/payment-gateways/{world.club.id}/disable", headers=world.admin_h)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "disabled" and r.json()["club"]["id"] == world.club.id and r.json()["key_id"] == OTHER_KEY
    assert (await client.post(f"{API}/admin/payment-gateways/{world.club.id}/disable", headers=world.admin_h)).status_code == 404

    # The club falls back to the platform, and can see why.
    page = (await client.get(GATEWAY, headers=world.h)).json()
    assert page["connected"] is False and page["status"] == "disabled" and page["key_id"] == OTHER_KEY
    assert page["can_manage"] is False and page["webhook_secret"] is None
    payer, reg = await _payer(session, make_user, make_registration, world.event)
    assert (await _order(client, _mint(world), payer, world.event, reg, checkout=None)).json()["gateway"] == "platform"
    assert (await client.get(f"{API}/events/info/{world.event.id}")).json()["payments_to"] == "platform"
    disconnected = await _alerts(session, "payment_gateway_disconnected")
    assert len(disconnected) == 1 and disconnected[0].club_id == world.club.id and disconnected[0].data["by"] == "admin"
    audit = await session.scalar(select(AdminAuditLogs).where(AdminAuditLogs.action == "payment_gateway.disable"))
    assert audit.actor_id == world.admin.id and audit.entity_id == str(world.club.id)

    # It holds until an admin lifts it: no new keys round it, no check, no disconnect.
    world.account.valid = True
    for method, path in (("PUT", GATEWAY), ("POST", f"{GATEWAY}/verify"), ("DELETE", GATEWAY)):
        kwargs = {"json": {"key_id": CLUB_KEY, "key_secret": CLUB_SECRET}} if method == "PUT" else {}
        r = await client.request(method, path, headers=world.h, **kwargs)
        assert r.status_code == 409 and r.json()["error_code"] == "GATEWAY_DISABLED", (method, r.text)
        assert r.json()["message"] == "MyOtherApp has turned off your own Razorpay. Talk to us."
    assert (await client.get(GATEWAY, headers=world.h)).json()["status"] == "disabled"


async def test_an_admin_lifts_a_disable_with_keys_that_still_work(client, session, world, make_admin, mint_token):
    out = await _connect(client, world.h)
    assert (await client.post(f"{API}/admin/payment-gateways/{world.club.id}/enable", headers=world.admin_h)).json()["error_code"] == "NOT_DISABLED"
    assert (await client.post(f"{API}/admin/payment-gateways/{world.rival.id}/enable", headers=world.admin_h)).status_code == 404
    await client.post(f"{API}/admin/payment-gateways/{world.club.id}/disable", headers=world.admin_h)
    viewer = await make_admin(email=f"viewer{next(_n)}@example.com", role=AdminRoles.viewer)
    r = await client.post(f"{API}/admin/payment-gateways/{world.club.id}/enable", headers=auth_header(await mint_token(viewer)))
    assert r.status_code == 403
    world.account.calls.clear()

    r = await client.post(f"{API}/admin/payment-gateways/{world.club.id}/enable", headers=world.admin_h)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "active" and r.json()["last_error"] is None and r.json()["key_id"] == CLUB_KEY
    assert world.account.calls == [("order.all",)]
    page = (await client.get(GATEWAY, headers=world.h)).json()
    # The same row, so the webhook already set up in Razorpay keeps working.
    assert page["connected"] is True and page["can_manage"] is True and page["webhook_url"] == out["webhook_url"]
    assert page["webhook_secret"] == out["webhook_secret"]
    assert (await client.get(f"{API}/events/info/{world.event.id}")).json()["payments_to"] == "club"
    connected = await _alerts(session, "payment_gateway_connected")
    assert [a.data["by"] for a in connected] == ["club", "admin"]
    audit = await session.scalar(select(AdminAuditLogs).where(AdminAuditLogs.action == "payment_gateway.enable"))
    assert audit.actor_id == world.admin.id and audit.details["result"] == "ok"


async def test_an_admin_lifts_a_disable_with_keys_that_no_longer_work(client, session, world):
    await _connect(client, world.h)
    await client.post(f"{API}/admin/payment-gateways/{world.club.id}/disable", headers=world.admin_h)
    world.account.valid = False

    r = await client.post(f"{API}/admin/payment-gateways/{world.club.id}/enable", headers=world.admin_h)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "failed" and "Authentication failed" in r.json()["last_error"]
    assert (await client.get(f"{API}/events/info/{world.event.id}")).json()["payments_to"] == "platform"
    assert [a.data["by"] for a in await _alerts(session, "payment_gateway_connected")] == ["club", "admin"]
    assert len(await _alerts(session, "payment_gateway_failed")) == 1
    page = (await client.get(GATEWAY, headers=world.h)).json()
    assert page["connected"] is True and page["status"] == "failed" and page["can_manage"] is True

    # No longer held: the club pastes working keys and is back on its own account.
    replaced = await _connect(client, world.h, OTHER_KEY, OTHER_SECRET)
    assert replaced["status"] == "active" and replaced["key_id"] == OTHER_KEY
    statuses = list((await session.scalars(select(ClubPaymentGateways.status).where(ClubPaymentGateways.club_id == world.club.id).order_by(ClubPaymentGateways.id))).all())
    assert statuses == ["replaced", "active"]


async def test_admin_payments_say_whose_account_took_the_money(client, session, world, make_user, make_registration):
    await _connect(client, world.h)
    payer, reg = await _payer(session, make_user, make_registration, world.event)
    club_order = (await _order(client, _mint(world), payer, world.event, reg)).json()
    rival_payer, rival_reg = await _payer(session, make_user, make_registration, world.rival_event, amount=40.0)
    rival_order = (await _order(client, _mint(world), rival_payer, world.rival_event, rival_reg)).json()
    for account, order in ((world.account, club_order), (world.platform, rival_order)):
        payment_id = account.pay(order["razorpay_order_id"], order["amount"])
        r = await client.post(f"{API}/payments/verify", json={"razorpay_order_id": order["razorpay_order_id"], "razorpay_payment_id": payment_id})
        assert r.status_code == 200, r.text

    totals = (await client.get(f"{API}/payments/admin/totals", headers=world.admin_h)).json()
    assert totals["club"]["captured_total"] == 100.0 and totals["platform"]["captured_total"] == 40.0
    assert totals["captured_total"] == 140.0 and totals["net_total"] == 140.0
    rows = (await client.get(f"{API}/payments/admin/list", headers=world.admin_h)).json()["items"]
    by_order = {row["razorpay_order_id"]: row for row in rows}
    assert by_order[club_order["razorpay_order_id"]]["gateway"] == "club"
    assert by_order[club_order["razorpay_order_id"]]["gateway_id"] is not None
    assert by_order[rival_order["razorpay_order_id"]]["gateway"] == "platform"
    only_club = (await client.get(f"{API}/payments/admin/list", params={"gateway": "club"}, headers=world.admin_h)).json()["items"]
    assert [row["razorpay_order_id"] for row in only_club] == [club_order["razorpay_order_id"]]


# ─── Keeping the token and the secret out of logs ───────────────────────


async def test_the_webhook_token_and_key_bodies_are_redacted():
    token = "AbCdEf" + "x" * 30
    assert redact.path(f"/api/v1/payments/webhook/club/{token}") == "/api/v1/payments/webhook/club/AbCdEf…"
    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                               ("1.2.3.4:5", "POST", f"/api/v1/payments/webhook/club/{token}", "1.1", 200), None)
    assert redact.AccessLogFilter().filter(record) and token not in record.getMessage()

    try:
        raise ValueError(f"something quoted {CLUB_SECRET}")
    except ValueError as exc:
        hidden = redact.error_traceback("/api/v1/clubs/payments/gateway", exc)
        shown = redact.error_traceback("/api/v1/events/info/1", exc)
    assert CLUB_SECRET not in hidden and "ValueError" in hidden and CLUB_SECRET in shown


async def test_the_error_report_never_carries_a_key_secret(monkeypatch):
    from app.core.utils import discord

    sent = []
    monkeypatch.setattr(settings, "DISCORD_ERROR_WEBHOOK", "https://discord.invalid/hook")

    async def capture(details, *a, **kw):
        sent.append(details)

    monkeypatch.setattr(discord, "send_discord_notification", capture)
    request = SimpleNamespace(url=SimpleNamespace(path="/api/v1/clubs/payments/gateway"), method="PUT")
    try:
        raise RuntimeError(f"boom {CLUB_SECRET}")
    except RuntimeError as exc:
        await discord.notify_error(request, exc, str(uuid.uuid4()))
    token = "Zz" * 20
    webhook = SimpleNamespace(url=SimpleNamespace(path=f"/api/v1/payments/webhook/club/{token}"), method="POST")
    try:
        raise RuntimeError("boom")
    except RuntimeError as exc:
        await discord.notify_error(webhook, exc, str(uuid.uuid4()))
    assert CLUB_SECRET not in json.dumps(sent) and token not in json.dumps(sent)


async def test_orders_made_before_a_gateway_and_platform_status_are_unchanged(session, world, make_user, make_registration):
    """The service called as before (no checkout flag) on a platform event."""
    payer, reg = await _payer(session, make_user, make_registration, world.rival_event)
    order = await payments.create_razorpay_order(
        session, source="event_registration",
        payload={"event_id": world.rival_event.id, "event_registration_id": str(reg.id)}, user_id=payer.id,
    )
    assert order.payment_gateway_id is None and order.status == OrderStatus.created
    assert await gateways.checkout_fields(session, order) == {"key_id": settings.RAZORPAY_KEY_ID, "gateway": "platform"}
    assert PaymentStatus.captured.value == "captured"
    assert await session.scalar(select(EventRegistrationsLink.payment_receipt).where(EventRegistrationsLink.id == reg.id)) == order.receipt
