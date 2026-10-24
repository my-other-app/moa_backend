"""A club's own WhatsApp number, further (app/api/club_whatsapp: delivery_routes,
spend, consent, broadcasts, embedded_signup, app_webhook).

Meta is never reached: `meta` (FakeMeta from test_club_whatsapp, taught a few
more of Graph's paths here -- pricing analytics, the Marketing Messages API,
/register, the code exchange) stands in for it and records what it was
asked. What is checked is what Meta would be asked, and what the club, the
deliveries and the admins see afterwards.
"""
import hashlib
import hmac
import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import func, select

from app.api.audit.models import AdminAuditLogs
from app.api.club_whatsapp import broadcasts, consent, crypto, delivery_routes, graph, optout, spend
from app.api.club_whatsapp import service as accounts
from app.api.club_whatsapp.models import ClubWhatsAppAccounts, ClubWhatsAppTemplates
from app.api.club_whatsapp.reach_models import (
    ClubWhatsAppBroadcastRecipients,
    ClubWhatsAppBroadcasts,
    ClubWhatsAppConsents,
    ClubWhatsAppSpend,
)
from app.api.deliveries import service as deliveries
from app.api.deliveries.models import Deliveries, DeliveryAttempts, DeliveryChannel, DeliveryKind, DeliveryStatus
from app.api.message_quota.models import MessageUsageRefs
from app.config import settings
from app.core import jobs, whatsapp
from tests.conftest import auth_header
from tests.test_club_whatsapp import (  # noqa: F401 - fixtures, used by name
    API,
    APP_ID,
    GUEST,
    KEYS,
    NOW,
    PHONE,
    SECRET,
    TICKET_BUTTON,
    WABA,
    FakeMeta,
    _enforced,
    _h,
    _tpl,
    account_of,
    connected,
    hook,
    inbound,
    mails,
    member,
    plans,
    platform_number,
    refused,
    sign,
    standard_templates,
    world,
)

ES_SECRET = "fedcba9876543210fedcba9876543210"
ES_TOKEN = "EAAGbusinessTOKENfromEMBEDDEDsignup" + "z" * 40 + "Bz42"
CONFIG_ID = "998877665544"
PLATFORM_VERIFY = "platform-app-verify-token"
CODE = "AQBhlXsctMxJYbwbrpybxlo9tLPGy-QAmjBJA03jxLos43wxlBlrYozY5C33"


def _marketing(name, body, *, language="en", header=None, button=None, status="APPROVED"):
    out = _tpl(name, body, language=language, header=header, button=button, status=status)
    out["category"] = "MARKETING"
    return out


def reach_templates():
    return [
        *standard_templates(),
        _tpl("event_news", "Hi {{1}}, news about {{2}} on {{3}}: {{4}}"),
        _marketing("club_offer", "Hi {{1}}, {{2}}", button={"type": "URL", "text": "See it", "url": "https://myotherapp.in/c/{{1}}"}),
        _marketing("doc_offer", "Hello {{1}}", header={"type": "HEADER", "format": "DOCUMENT"}),
    ]


class ReachMeta(FakeMeta):
    """FakeMeta, and Graph's paths this feature asks."""

    def __init__(self):
        super().__init__()
        self.templates = reach_templates()
        self.currency = "INR"
        self.points = [
            {"start": 1, "end": 2, "pricing_category": "MARKETING", "pricing_type": "REGULAR", "volume": 120, "cost": 103.572},
            {"start": 2, "end": 3, "pricing_category": "MARKETING", "pricing_type": "REGULAR", "volume": 30, "cost": 25.893},
            {"start": 1, "end": 2, "pricing_category": "UTILITY", "pricing_type": "REGULAR", "volume": 400, "cost": 46.0},
            {"start": 1, "end": 2, "pricing_category": "SERVICE", "pricing_type": "FREE_CUSTOMER_SERVICE", "volume": 12, "cost": 0},
        ]
        self.mm_status = "ELIGIBLE"
        self.marketing_sent: list[dict] = []
        self.marketing_answer = None
        self.subscribed: list = []
        self.registered: list[dict] = []
        self.register_answer = None
        self.exchanged: list[dict] = []
        self.exchange_answer = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        request.read()
        path = re.sub(r"^/v\d+\.\d+/", "", request.url.path)
        key = f"{request.method} {path}"
        if request.url.host != "graph.facebook.com" or key in self.fail:
            return super().handler(request)
        params = dict(request.url.params)
        if path == "oauth/access_token":
            self.calls.append(request)
            self.exchanged.append(params)
            if self.exchange_answer is not None:
                return httpx.Response(self.exchange_answer[0], json=self.exchange_answer[1])
            return httpx.Response(200, json={"access_token": ES_TOKEN, "token_type": "bearer"})
        if key == f"GET {WABA}":
            self.calls.append(request)
            fields = params.get("fields") or ""
            if "pricing_analytics" in fields:
                return httpx.Response(200, json={"currency": self.currency, "id": WABA,
                                                 "pricing_analytics": {"data": [{"data_points": self.points}]}})
            if "marketing_messages_onboarding_status" in fields:
                return httpx.Response(200, json={"marketing_messages_onboarding_status": self.mm_status, "id": WABA})
        if key == f"POST {PHONE}/marketing_messages":
            self.calls.append(request)
            body = json.loads(request.content)
            self.marketing_sent.append(body)
            if self.marketing_answer is not None:
                return self.marketing_answer(body)
            return httpx.Response(200, json={"messages": [{"id": f"wamid.MM{len(self.marketing_sent)}"}]})
        if key == f"POST {PHONE}/register":
            self.calls.append(request)
            body = json.loads(request.content)
            self.registered.append(body)
            if self.register_answer is not None:
                return self.register_answer(body)
            return httpx.Response(200, json={"success": True})
        if key == f"POST {WABA}/subscribed_apps":
            self.subscribed.append(json.loads(request.content) if request.content else None)
        return super().handler(request)

    def analytics_calls(self) -> int:
        return sum(1 for r in self.calls if r.method == "GET" and "pricing_analytics" in (dict(r.url.params).get("fields") or ""))

    def mm_status_calls(self) -> int:
        return sum(1 for r in self.calls if r.method == "GET" and "marketing_messages_onboarding_status" in (dict(r.url.params).get("fields") or ""))


@pytest.fixture
def meta(monkeypatch):
    fake = ReachMeta()
    monkeypatch.setattr(settings, "WHATSAPP_KEYS_SECRET", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "WHATSAPP_GRAPH_VERSION", "v23.0", raising=False)
    monkeypatch.setattr(settings, "CLUB_WHATSAPP_BROADCAST_MPS", 0.0)
    monkeypatch.setattr(graph, "_client", lambda timeout=None: httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)))
    monkeypatch.setattr(whatsapp, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)))
    return fake


@pytest.fixture
def es_on(monkeypatch):
    """Embedded Signup turned on, with the platform app's settings."""
    monkeypatch.setattr(settings, "WHATSAPP_EMBEDDED_SIGNUP", True)
    monkeypatch.setattr(settings, "META_APP_ID", APP_ID)
    monkeypatch.setattr(settings, "META_APP_SECRET", ES_SECRET)
    monkeypatch.setattr(settings, "META_ES_CONFIG_ID", CONFIG_ID)
    monkeypatch.setattr(settings, "META_WEBHOOK_VERIFY_TOKEN", PLATFORM_VERIFY)


async def _whatsapp_row(session, reg_id, kind=DeliveryKind.reminder) -> Deliveries:
    return await session.scalar(select(Deliveries).where(
        Deliveries.registration_id == reg_id, Deliveries.kind == kind, Deliveries.channel == DeliveryChannel.whatsapp,
    ).execution_options(populate_existing=True))


async def _reminder_for(session, event, reg) -> Deliveries:
    reg.created_at = NOW - timedelta(days=10)
    await session.commit()
    await deliveries.queue(session, event, DeliveryKind.reminder, registration_ids=[reg.id])
    row = await _whatsapp_row(session, reg.id)
    return await deliveries.deliver(session, row.id)


async def _guest(session, w, make_user, make_registration, email, phone, *, opted=True, event=None, **extra):
    user = await make_user(email=email)
    reg = await make_registration(event=event or w.event, user=user, full_name=extra.pop("full_name", "Guest Person"), email=email,
                                  phone=phone, whatsapp_opt_in=opted, **extra)
    await session.commit()
    return reg


# ─── Routing by kind; which number, and why ──────────────────────────────


async def test_each_kind_has_its_own_switch_under_routing(client, session, world, meta, platform_number):
    await connected(client, session, world)
    reach = (await client.get(f"{API}/clubs/whatsapp/account/reach", headers=world.h)).json()
    assert reach["routing"] is True and reach["kinds"] == {"tickets": True, "certificates": True, "reminders": True}
    assert reach["embedded_signup"] is None and reach["consent_text"] == "Get updates from Yukthi on WhatsApp"

    r = await client.patch(f"{API}/clubs/whatsapp/account/routing", json={"reminders": False}, headers=world.h)
    assert r.status_code == 200, r.text
    assert r.json()["kinds"] == {"tickets": True, "certificates": True, "reminders": False}
    assert await accounts.route_for(session, world.club.id, "event_reminder") is None
    assert await accounts.route_for(session, world.club.id, "ticket_confirmed") is not None
    audit = await session.scalar(select(AdminAuditLogs).where(AdminAuditLogs.action == "club_whatsapp.settings"))
    assert audit.details["after"]["kinds"]["reminders"] is False

    # Reminders off: from MyOtherApp's number, the club's choice -- not a fallback.
    d = await _reminder_for(session, world.event, world.reg)
    assert d.status == DeliveryStatus.sent and d.provider_id == "wamid.PLATFORM1" and meta.club_sent == []
    assert d.via == "platform" and d.fallback_code is None and d.fallback_reason is None

    # On again: the next reminder goes from the club's own.
    assert (await client.patch(f"{API}/clubs/whatsapp/account/routing", json={"reminders": True}, headers=world.h)).status_code == 200
    d.status = DeliveryStatus.failed
    await session.commit()
    await deliveries.queue(session, world.event, DeliveryKind.reminder, registration_ids=[world.reg.id], only="failed")
    d = await deliveries.deliver(session, d.id)
    assert d.provider_id == "wamid.CLUB1" and d.via == "club" and d.fallback_code is None
    attempt = await session.scalar(select(DeliveryAttempts).where(DeliveryAttempts.provider_id == "wamid.CLUB1"))
    assert attempt.via == "club" and attempt.fallback_code is None

    # The master switch still rules them all.
    assert (await client.patch(f"{API}/clubs/whatsapp/account", json={"routing": False}, headers=world.h)).status_code == 200
    assert await accounts.route_for(session, world.club.id, "ticket_confirmed") is None


async def test_only_who_manages_the_number_switches_kinds(client, session, world, meta):
    await connected(client, session, world)
    editor = await member(session, world, "editor", whatsapp_permission=True)
    r = await client.patch(f"{API}/clubs/whatsapp/account/routing", json={"tickets": False}, headers=_h(editor))
    assert r.status_code == 403
    assert (await client.get(f"{API}/clubs/whatsapp/account/reach", headers=_h(editor))).status_code == 200
    admin = await member(session, world, "admin")
    assert (await client.patch(f"{API}/clubs/whatsapp/account/routing", json={"tickets": False}, headers=_h(admin))).status_code == 200


async def test_a_refusal_from_the_clubs_number_is_kept_as_the_fallbacks_reason(client, session, world, meta, platform_number,
                                                                               make_user, make_registration):
    await connected(client, session, world)
    meta.club_answer = refused(132015, "Template is paused")
    d = await _reminder_for(session, world.event, world.reg)
    assert d.status == DeliveryStatus.sent and d.provider_id == "wamid.PLATFORM1"
    assert d.via == "platform" and d.fallback_code == "132015" and "paused" in d.fallback_reason
    attempt = await session.scalar(select(DeliveryAttempts).where(DeliveryAttempts.provider_id == "wamid.PLATFORM1"))
    assert attempt.via == "platform" and attempt.fallback_code == "132015" and "paused" in attempt.fallback_reason

    # The template is unusable now: the next guest's goes from the platform's, saying so.
    other = await _guest(session, world, make_user, make_registration, "two@example.com", "9000000002")
    d2 = await _reminder_for(session, world.event, other)
    assert d2.provider_id == "wamid.PLATFORM2" and d2.fallback_code == "TEMPLATE_UNUSABLE"
    assert "event_reminder" in d2.fallback_reason

    # The deliveries screen and the attendee list say which number, and why.
    log = (await client.get(f"{API}/events/{world.event.id}/deliveries?channel=whatsapp", headers=world.h)).json()
    rows = {i["registration_id"]: i for i in log["items"]}
    assert rows[str(world.reg.id)]["via"] == "platform" and rows[str(world.reg.id)]["fallback_code"] == "132015"
    briefs = await deliveries.by_registration(session, world.event.id, DeliveryKind.reminder, [other.id])
    wa = next(b for b in briefs[other.id] if b["channel"] == "whatsapp")
    assert wa["via"] == "platform" and wa["fallback_code"] == "TEMPLATE_UNUSABLE"
    assert next(b for b in briefs[other.id] if b["channel"] == "email")["via"] is None


async def test_no_approved_template_is_a_reason_too_and_a_club_send_has_none(client, session, world, meta, platform_number):
    meta.templates = [t for t in reach_templates() if t["name"] != "event_reminder"]
    await connected(client, session, world)
    d = await _reminder_for(session, world.event, world.reg)
    assert d.provider_id == "wamid.PLATFORM1" and d.via == "platform" and d.fallback_code == "NO_TEMPLATE"
    assert await delivery_routes.why_not(session, world.club.id, "event_reminder") == (
        "NO_TEMPLATE", "Your account has no approved event_reminder template in the shape MyOtherApp sends.")
    # A club with no number of its own: from the platform's, with no reason to give.
    assert await delivery_routes.why_not(session, world.other_club.id, "event_reminder") is None


async def test_metas_later_refusal_keeps_its_code_when_the_platform_sends_it_again(client, session, world, meta, platform_number, mails):
    await connected(client, session, world)
    d = await _reminder_for(session, world.event, world.reg)
    assert d.provider_id == "wamid.CLUB1" and d.via == "club"
    account = await account_of(session, world.club.id)
    st = {"id": "wamid.CLUB1", "status": "failed", "timestamp": str(int(NOW.timestamp())), "recipient_id": GUEST,
          "errors": [{"code": 131042, "title": "Business eligibility payment issue"}]}
    assert (await hook(client, account.webhook_token, inbound(statuses=[st]))).status_code == 200
    d = await session.get(Deliveries, d.id, populate_existing=True)
    assert d.status == DeliveryStatus.pending and d.fallback_code == "131042"
    d = await deliveries.deliver(session, d.id)
    assert d.provider_id == "wamid.PLATFORM1" and d.via == "platform" and d.fallback_code == "131042"


# ─── Spend: read by the sweep, never by a page ───────────────────────────


async def test_spend_is_read_by_the_sweep_at_most_daily_and_never_on_a_page_view(client, session, world, meta, platform_number):
    await connected(client, session, world)
    page = (await client.get(f"{API}/clubs/whatsapp/spend", headers=world.h)).json()
    assert page["fetched_at"] is None and page["categories"] == [] and page["approximate"] is True and page["note"]
    assert meta.analytics_calls() == 0

    now = datetime.now(timezone.utc)
    assert await spend.refresh(session, now) == 1
    assert meta.analytics_calls() == 1
    asked = next(r for r in meta.calls if "pricing_analytics" in (dict(r.url.params).get("fields") or ""))
    fields = dict(asked.url.params)["fields"]
    start = int(delivery_routes.month_start(now).timestamp())
    assert f"pricing_analytics.start({start}).end(" in fields and "granularity(DAILY)" in fields
    assert "metric_types(COST,VOLUME)" in fields and "dimensions(PRICING_CATEGORY)" in fields and fields.startswith("currency,")
    assert asked.headers["authorization"].startswith("Bearer ")

    for _ in range(3):
        page = (await client.get(f"{API}/clubs/whatsapp/spend", headers=world.h)).json()
    assert meta.analytics_calls() == 1
    assert page["currency"] == "INR" and page["fetched_at"] is not None
    cats = {c["category"]: c for c in page["categories"]}
    assert cats["MARKETING"] == {"category": "MARKETING", "volume": 150, "cost": 129.465}
    assert cats["UTILITY"]["volume"] == 400 and cats["SERVICE"]["cost"] == 0
    assert [c["category"] for c in page["categories"]] == ["MARKETING", "UTILITY", "SERVICE"]
    assert page["volume"] == 562 and page["cost"] == pytest.approx(175.465)

    # The same day: nothing asked. A day on: asked once more.
    assert await spend.refresh(session, now + timedelta(hours=3)) == 0
    assert await spend.refresh(session, now + timedelta(hours=25)) == 1
    assert meta.analytics_calls() == 2

    # A new month: the month before read once more, for its last days, and the new one.
    account = await account_of(session, world.club.id)
    next_month = delivery_routes.month_start(delivery_routes.month_start(now) + timedelta(days=35)) + timedelta(hours=2)
    account.spend_checked_at = next_month - timedelta(days=2)
    await session.commit()
    assert await spend.refresh(session, next_month) == 1
    assert meta.analytics_calls() == 4
    assert await session.scalar(select(func.count()).select_from(ClubWhatsAppSpend)) == 2


async def test_spend_counts_the_clubs_sends_and_its_fallbacks_by_reason(client, session, world, meta, platform_number,
                                                                        make_user, make_registration):
    await connected(client, session, world)
    d = await _reminder_for(session, world.event, world.reg)
    assert d.via == "club"
    meta.club_answer = refused(132015, "Template is paused")
    other = await _guest(session, world, make_user, make_registration, "three@example.com", "9000000003")
    await _reminder_for(session, world.event, other)
    third = await _guest(session, world, make_user, make_registration, "four@example.com", "9000000004")
    await _reminder_for(session, world.event, third)
    page = (await client.get(f"{API}/clubs/whatsapp/spend", headers=world.h)).json()
    assert page["from_your_number"]["deliveries"] == 1 and page["fallback_total"] == 2
    assert {f["code"]: f["count"] for f in page["fallbacks"]} == {"132015": 1, "TEMPLATE_UNUSABLE": 1}
    assert meta.analytics_calls() == 0


async def test_the_admins_see_fallbacks_on_the_club_and_the_clubs_with_most(client, session, world, meta, platform_number,
                                                                            make_admin, mint_token):
    await connected(client, session, world)
    meta.club_answer = refused(132015, "Template is paused")
    await _reminder_for(session, world.event, world.reg)
    admin = await make_admin(email="wa-admin@example.com")
    h = auth_header(await mint_token(admin))
    card = (await client.get(f"{API}/admin/club-whatsapp/{world.club.id}", headers=h)).json()
    assert card["fallbacks_this_month"] == 1 and card["club_sends_this_month"] == 0
    top = (await client.get(f"{API}/admin/club-whatsapp/fallbacks", headers=h))
    assert top.status_code == 200, top.text
    items = top.json()["items"]
    assert items == [{"club_id": world.club.id, "club_name": "Yukthi", "fallbacks": 1, "top_code": "132015",
                      "top_reason": items[0]["top_reason"]}] and "paused" in items[0]["top_reason"]
    assert (await client.get(f"{API}/admin/club-whatsapp/fallbacks", headers=world.h)).status_code in (401, 403)


# ─── Consent: the field, and taking it back ──────────────────────────────


async def test_the_registration_form_keeps_the_yes_to_the_clubs_updates(client, session, world, meta):
    url = f"{API}/events/registration/{world.event.id}/register"
    r = await client.post(url, json={"full_name": "Kiran Rao", "email": "kiran@example.com", "phone": "98765 22222",
                                     "club_updates_whatsapp": True})
    assert r.status_code == 200, r.text
    assert r.json()["club_updates_whatsapp"] is True
    row = await session.scalar(select(ClubWhatsAppConsents).where(ClubWhatsAppConsents.club_id == world.club.id))
    assert row.source == "registration" and row.wa_id == "919876522222" and row.withdrawn_at is None
    assert row.consent_text == "Get updates from Yukthi on WhatsApp" and row.text_version == consent.TEXT_VERSION
    assert str(row.registration_id) == r.json()["event_registration_id"]

    # Unticked or left out: nothing kept, nothing taken back.
    r = await client.post(url, json={"full_name": "Lata Iyer", "email": "lata@example.com", "phone": "98765 33333"})
    assert r.status_code == 200 and r.json()["club_updates_whatsapp"] is False
    # Ticked with no number WhatsApp reaches: said before anything is kept.
    bad = await client.post(url, json={"full_name": "Mohan Das", "email": "mohan@example.com", "phone": "12345", "club_updates_whatsapp": True})
    assert bad.status_code == 422 and bad.json()["error_code"] == "WHATSAPP_NUMBER" and "phone" in bad.json()["errors"]
    assert await session.scalar(select(func.count()).select_from(ClubWhatsAppConsents)) == 1


async def test_following_says_yes_or_no_and_unfollowing_takes_it_back(client, session, world, meta, make_user, mint_token):
    fan = await make_user(email="fan@example.com")
    fan.phone = "+91 98765 44444"
    await session.commit()
    h = auth_header(await mint_token(fan))
    r = await client.post(f"{API}/clubs/follow/{world.club.id}", json={"club_updates_whatsapp": True}, headers=h)
    assert r.status_code == 200, r.text
    assert await consent.live(session, world.club.id, user_id=fan.id)
    row = await session.scalar(select(ClubWhatsAppConsents).where(ClubWhatsAppConsents.user_id == fan.id))
    assert row.source == "follow" and row.wa_id == "919876544444"
    # Already following: the follow as it stands, the yes kept once.
    r = await client.post(f"{API}/clubs/follow/{world.club.id}", json={"club_updates_whatsapp": True}, headers=h)
    assert r.status_code == 200 and r.json()["is_following"] is True
    assert await session.scalar(select(func.count()).select_from(ClubWhatsAppConsents).where(ClubWhatsAppConsents.withdrawn_at.is_(None))) == 1
    # Without the field: a follow as ever (already following is a 409).
    assert (await client.post(f"{API}/clubs/follow/{world.club.id}", headers=h)).status_code == 409
    r = await client.post(f"{API}/clubs/unfollow/{world.club.id}", headers=h)
    assert r.status_code == 200
    row = await session.scalar(select(ClubWhatsAppConsents).where(ClubWhatsAppConsents.user_id == fan.id)
                               .execution_options(populate_existing=True))
    assert row.withdrawn_at is not None and row.withdrawn_source == "unfollow"
    assert not await consent.live(session, world.club.id, user_id=fan.id)
    # Following again with false: no yes.
    r = await client.post(f"{API}/clubs/follow/{world.club.id}", json={"club_updates_whatsapp": False}, headers=h)
    assert r.status_code == 200 and not await consent.live(session, world.club.id, user_id=fan.id)


# ─── Broadcasts ──────────────────────────────────────────────────────────

UPDATE = {
    "template": "event_news", "language": "en", "event_id": None,
    "variables": {"body": [{"source": "name"}, {"source": "event_name"}, {"source": "event_date"},
                           {"source": "text", "value": "The hall moved  to Lab 3."}]},
}


def update_for(w, **extra) -> dict:
    return {**UPDATE, "event_id": w.event.id, **extra}


OFFER = {"template": "club_offer", "language": "en",
         "variables": {"body": [{"source": "name"}, {"source": "text", "value": "20% off our workshop this week."}],
                       "buttons": {"0": {"source": "text", "value": "workshop-26"}}}}


async def _consent(session, w, make_user, email, phone, *, name="Fan Person"):
    user = await make_user(email=email)
    user.full_name, user.phone = name, phone
    await session.commit()
    await consent.give(session, w.club.id, source="follow", club_name="Yukthi", user_id=user.id, phone=phone)
    await session.commit()
    return user


async def test_an_event_update_reaches_the_registrants_who_asked_for_whatsapp(client, session, world, meta, make_user,
                                                                             make_registration, monkeypatch):
    await connected(client, session, world)
    await _guest(session, world, make_user, make_registration, "b@example.com", "9000000005", full_name="Bala Krishnan")
    await _guest(session, world, make_user, make_registration, "c@example.com", "9000000006", opted=False)  # never asked
    await _guest(session, world, make_user, make_registration, "d@example.com", "12345")  # no number WhatsApp reaches
    await _guest(session, world, make_user, make_registration, "e@example.com", "98765 43210")  # the same number again: once
    await _guest(session, world, make_user, make_registration, "f@example.com", "9000000007", ticket_revoked_at=NOW)  # no ticket
    await _guest(session, world, make_user, make_registration, "g@example.com", "9000000008")  # asked to stop

    async def blocked(session, club_id, *, wa_id=None, bsuid=None, scope="all"):
        return wa_id == "919000000008"

    monkeypatch.setattr(optout, "blocked", blocked)
    usage = await session.scalar(select(func.count()).select_from(MessageUsageRefs))

    preview = await client.post(f"{API}/clubs/whatsapp/broadcasts/preview", json=update_for(world), headers=world.h)
    assert preview.status_code == 200, preview.text
    p = preview.json()
    assert p["kind"] == "event_update" and p["category"] == "UTILITY" and p["recipients"] == 2
    assert p["excluded"] == {"opted_out": 1, "no_consent": 1, "no_number": 1}
    est = p["estimate"]
    assert est["rate"] == 0.115 and est["currency"] == "INR" and est["subtotal"] == 0.23 and est["gst"] == 0.04 and est["total"] == 0.27
    assert est["estimate"] is True and "estimate" in est["label"] and est["messaging_limit"]["per_day"] == 1000
    assert re.match(r"Hi (Ananya|Bala), news about Build Night on .+: The hall moved to Lab 3\.$", p["sample"]), p["sample"]
    assert meta.club_sent == []

    r = await client.post(f"{API}/clubs/whatsapp/broadcasts", json=update_for(world), headers=world.h)
    assert r.status_code == 200, r.text
    out = r.json()
    # Sent by its job, which ran as the request answered.
    b = await session.get(ClubWhatsAppBroadcasts, out["id"], populate_existing=True)
    assert b.status == "done" and b.endpoint == "messages"
    by_number = {m["to"]: m["template"] for m in meta.club_sent}
    assert set(by_number) == {GUEST, "919000000005"} and len(meta.club_sent) == 2 and meta.marketing_sent == []
    sent = by_number[GUEST]
    assert sent["name"] == "event_news" and sent["language"] == {"code": "en"}
    values = [p["text"] for p in sent["components"][0]["parameters"]]
    assert values[0] == "Ananya" and values[1] == "Build Night" and values[3] == "The hall moved to Lab 3." and re.match(r"\w{3} \d+ \w{3} \d{4}", values[2])
    assert by_number["919000000005"]["components"][0]["parameters"][0]["text"] == "Bala"
    page = (await client.get(f"{API}/clubs/whatsapp/broadcasts/{out['id']}", headers=world.h)).json()
    assert page["results"]["sent"] == 2 and page["results"]["skipped"]["opted_out"] == 1
    assert page["results"]["skipped"]["no_consent"] == 1 and page["results"]["skipped"]["no_number"] == 1
    assert page["event"] == {"id": world.event.id, "name": "Build Night"} and page["created_by"]
    # The club pays Meta: nothing counted against MyOtherApp's quotas.
    assert await session.scalar(select(func.count()).select_from(MessageUsageRefs)) == usage
    audit = await session.scalar(select(AdminAuditLogs).where(AdminAuditLogs.action == "club_whatsapp.broadcast"))
    assert audit.details["recipients"] == 2 and "Lab 3" not in json.dumps(audit.details) and "Lab 3" not in (audit.summary or "")
    listed = (await client.get(f"{API}/clubs/whatsapp/broadcasts", headers=world.h)).json()
    assert listed["total"] == 1 and listed["items"][0]["id"] == out["id"]


async def test_a_promotion_goes_only_to_those_who_said_yes_and_still_mean_it(client, session, world, meta, make_user, mint_token):
    await connected(client, session, world)
    await _consent(session, world, make_user, "p1@example.com", "9000000011", name="Priya Nair")
    gone = await _consent(session, world, make_user, "p2@example.com", "9000000012")
    await consent.withdraw(session, world.club.id, source="unfollow", user_id=gone.id)
    await session.commit()
    # Following without the box: no yes.
    plain = await make_user(email="p3@example.com")
    plain.phone = "9000000013"
    await session.commit()
    assert (await client.post(f"{API}/clubs/follow/{world.club.id}", headers=auth_header(await mint_token(plain)))).status_code == 200

    p = (await client.post(f"{API}/clubs/whatsapp/broadcasts/preview", json=OFFER, headers=world.h)).json()
    assert p["kind"] == "promotion" and p["recipients"] == 1 and p["estimate"]["rate"] == 0.8631
    assert p["estimate"]["subtotal"] == 0.86 and p["estimate"]["gst"] == 0.16 and p["estimate"]["total"] == 1.02
    r = await client.post(f"{API}/clubs/whatsapp/broadcasts", json=OFFER, headers=world.h)
    assert r.status_code == 200, r.text
    assert [m["to"] for m in meta.club_sent] == ["919000000011"]
    t = meta.club_sent[0]["template"]
    assert t["components"][0]["parameters"][0]["text"] == "Priya"
    assert t["components"][1] == {"type": "button", "sub_type": "url", "index": "0", "parameters": [{"type": "text", "text": "workshop-26"}]}
    # Not onboarded to Meta's Marketing Messages API: /messages, and the status read once.
    assert meta.mm_status_calls() == 1 and meta.marketing_sent == []


async def test_a_promotion_uses_metas_marketing_messages_api_once_onboarded(client, session, world, meta, make_user):
    await connected(client, session, world)
    meta.mm_status = "ONBOARDED"
    await _consent(session, world, make_user, "m1@example.com", "9000000021")
    assert (await client.post(f"{API}/clubs/whatsapp/broadcasts", json=OFFER, headers=world.h)).status_code == 200
    assert [m["to"] for m in meta.marketing_sent] == ["919000000021"] and meta.club_sent == []
    assert meta.marketing_sent[0]["template"]["name"] == "club_offer"
    # Read at most daily.
    assert (await client.post(f"{API}/clubs/whatsapp/broadcasts", json=OFFER, headers=world.h)).status_code == 200
    assert meta.mm_status_calls() == 1 and len(meta.marketing_sent) == 2
    # An event update is never marketing: /messages.
    assert (await client.post(f"{API}/clubs/whatsapp/broadcasts", json=update_for(world), headers=world.h)).status_code == 200
    assert meta.club_sent[-1]["template"]["name"] == "event_news"


async def test_131063_moves_a_promotion_to_the_marketing_messages_api(client, session, world, meta, make_user):
    await connected(client, session, world)
    for i in range(2):
        await _consent(session, world, make_user, f"x{i}@example.com", f"900000003{i}")
    meta.club_answer = lambda body: (httpx.Response(400, json={"error": {"code": 131063, "message": "(#131063) Marketing templates disabled for Cloud API"}})
                                     if body.get("type") == "template" and body["template"]["name"] == "club_offer"
                                     else httpx.Response(200, json={"messages": [{"id": "wamid.X"}]}))
    r = await client.post(f"{API}/clubs/whatsapp/broadcasts", json=OFFER, headers=world.h)
    b = await session.get(ClubWhatsAppBroadcasts, r.json()["id"], populate_existing=True)
    assert b.endpoint == "marketing_messages" and b.status == "done"
    # The first tried /messages once, then Meta's MM API; the second went there straight.
    assert len(meta.club_sent) == 1 and [m["to"] for m in meta.marketing_sent] == ["919000000030", "919000000031"]
    assert (await client.get(f"{API}/clubs/whatsapp/broadcasts/{b.id}", headers=world.h)).json()["results"]["sent"] == 2


async def test_the_messaging_limit_holds_the_rest_for_the_next_day(client, session, world, meta, make_user, make_registration, monkeypatch):
    await connected(client, session, world)
    monkeypatch.setitem(broadcasts.PER_DAY, "TIER_1K", 2)
    for i in range(3):
        await _guest(session, world, make_user, make_registration, f"l{i}@example.com", f"900000004{i}")
    p = (await client.post(f"{API}/clubs/whatsapp/broadcasts/preview", json=update_for(world), headers=world.h)).json()
    assert p["recipients"] == 4 and p["estimate"]["messaging_limit"] == {"tier": "TIER_1K", "per_day": 2, "used_today": 0, "today": 2, "days": 2}
    r = await client.post(f"{API}/clubs/whatsapp/broadcasts", json=update_for(world), headers=world.h)
    bid = r.json()["id"]
    assert len(meta.club_sent) == 2
    page = (await client.get(f"{API}/clubs/whatsapp/broadcasts/{bid}", headers=world.h)).json()
    assert page["status"] == "sending" and page["results"]["sent"] == 2 and page["results"]["waiting"] == 2
    assert page["results"]["skipped"]["limit_spill"] == 2
    held = (await session.scalars(select(ClubWhatsAppBroadcastRecipients).where(
        ClubWhatsAppBroadcastRecipients.broadcast_id == bid, ClubWhatsAppBroadcastRecipients.status == "queued"))).all()
    assert len(held) == 2 and all(h.held and h.not_before > datetime.now(timezone.utc) + timedelta(hours=23) for h in held)
    later = await session.scalar(select(jobs.Jobs).where(jobs.Jobs.name == broadcasts.JOB, jobs.Jobs.finished_at.is_(None)))
    assert later is not None and later.run_at > datetime.now(timezone.utc) + timedelta(hours=23)
    # A day on, the rest.
    out = await broadcasts.run(session, bid, now=datetime.now(timezone.utc) + timedelta(hours=25))
    assert out["sent"] == 2 and out["status"] == "done" and len(meta.club_sent) == 4


async def test_131049_waits_a_day_and_is_tried_once_more(client, session, world, meta, make_user):
    await connected(client, session, world)
    await _consent(session, world, make_user, "c1@example.com", "9000000051")
    meta.club_answer = refused(131049, "This message was not delivered to maintain healthy ecosystem engagement.")
    r = await client.post(f"{API}/clubs/whatsapp/broadcasts", json=OFFER, headers=world.h)
    bid = r.json()["id"]
    row = await session.scalar(select(ClubWhatsAppBroadcastRecipients).where(ClubWhatsAppBroadcastRecipients.broadcast_id == bid)
                               .execution_options(populate_existing=True))
    assert row.status == "queued" and row.capped == 1 and row.error_code == "131049"
    assert row.not_before > datetime.now(timezone.utc) + timedelta(hours=23)
    # Not again within the day.
    assert (await broadcasts.run(session, bid, now=datetime.now(timezone.utc) + timedelta(hours=2)))["sent"] == 0
    assert len(meta.club_sent) == 1
    # A day on: once more; a second 131049 is the end of it.
    out = await broadcasts.run(session, bid, now=datetime.now(timezone.utc) + timedelta(hours=25))
    await session.refresh(row)
    assert out["status"] == "done" and row.status == "failed" and row.error_code == "131049" and len(meta.club_sent) == 2


async def test_metas_webhook_says_how_each_went_and_131049_and_131050_by_webhook(client, session, world, meta, make_user):
    account = await connected(client, session, world)
    for i in range(3):
        await _consent(session, world, make_user, f"w{i}@example.com", f"900000006{i}")
    r = await client.post(f"{API}/clubs/whatsapp/broadcasts", json=OFFER, headers=world.h)
    bid = r.json()["id"]
    at = str(int(NOW.timestamp()))
    statuses = [
        {"id": "wamid.CLUB1", "status": "read", "timestamp": at, "recipient_id": "919000000060"},
        {"id": "wamid.CLUB1", "status": "delivered", "timestamp": at, "recipient_id": "919000000060"},  # late: never undoes read
        {"id": "wamid.CLUB2", "status": "failed", "timestamp": at, "recipient_id": "919000000061",
         "errors": [{"code": 131050, "title": "Unable to deliver the message"}]},
        {"id": "wamid.CLUB3", "status": "failed", "timestamp": at, "recipient_id": "919000000062",
         "errors": [{"code": 131049, "title": "Not delivered to maintain healthy ecosystem engagement"}]},
    ]
    assert (await hook(client, account.webhook_token, inbound(statuses=statuses))).status_code == 200
    res = (await client.get(f"{API}/clubs/whatsapp/broadcasts/{bid}", headers=world.h)).json()["results"]
    assert res["read"] == 1 and res["delivered"] == 1 and res["sent"] == 1
    assert res["failed"] == 1 and res["failed_by_reason"][0]["code"] == "131050" and res["waiting"] == 1
    # 131050: they stopped the club's marketing, so their yes is withdrawn.
    assert not await consent.live(session, world.club.id, wa_id="919000000061")
    row = await session.scalar(select(ClubWhatsAppConsents).where(ClubWhatsAppConsents.wa_id == "919000000061"))
    assert row.withdrawn_source == "meta_131050"
    p = (await client.post(f"{API}/clubs/whatsapp/broadcasts/preview", json=OFFER, headers=world.h)).json()
    assert p["recipients"] == 2
    # 131049 by webhook: back in the queue for a day later, the broadcast sending again.
    capped = await session.scalar(select(ClubWhatsAppBroadcastRecipients).where(ClubWhatsAppBroadcastRecipients.wa_id == "919000000062")
                                  .execution_options(populate_existing=True))
    assert capped.status == "queued" and capped.capped == 1 and capped.wamid is None
    assert (await session.get(ClubWhatsAppBroadcasts, bid, populate_existing=True)).status == "sending"


async def test_not_now_is_tried_again_and_an_account_refusal_stops_the_broadcast(client, session, world, meta, make_user, mails):
    await connected(client, session, world)
    for i in range(3):
        await _consent(session, world, make_user, f"r{i}@example.com", f"900000007{i}")
    answers = iter([httpx.Response(429, json={"error": {"code": 130429, "message": "Rate limit hit"}})])
    meta.club_answer = lambda body: next(answers, httpx.Response(200, json={"messages": [{"id": f"wamid.R{len(meta.club_sent)}"}]}))
    r = await client.post(f"{API}/clubs/whatsapp/broadcasts", json=OFFER, headers=world.h)
    bid = r.json()["id"]
    first = await session.scalar(select(ClubWhatsAppBroadcastRecipients).where(ClubWhatsAppBroadcastRecipients.wa_id == "919000000070")
                                 .execution_options(populate_existing=True))
    assert first.status == "queued" and first.error_code == "130429" and first.tries == 1 and first.not_before is not None
    out = await broadcasts.run(session, bid, now=datetime.now(timezone.utc) + timedelta(minutes=5))
    assert out["sent"] == 1 and out["status"] == "done"

    # The account refused: the broadcast stops and the number is marked failed.
    meta.club_answer = refused(190, "Error validating access token", status=401)
    r = await client.post(f"{API}/clubs/whatsapp/broadcasts", json=OFFER, headers=world.h)
    b = await session.get(ClubWhatsAppBroadcasts, r.json()["id"], populate_existing=True)
    assert b.status == "failed" and "190" in b.last_error
    res = (await client.get(f"{API}/clubs/whatsapp/broadcasts/{b.id}", headers=world.h)).json()["results"]
    assert res["failed"] == 1 and res["skipped"]["not_sent"] == 2
    assert (await account_of(session, world.club.id)).status == "failed"


async def test_what_fills_a_template_is_checked(client, session, world, meta):
    await connected(client, session, world)
    url = f"{API}/clubs/whatsapp/broadcasts/preview"
    few = {**update_for(world), "variables": {"body": [{"source": "name"}]}}
    r = await client.post(url, json=few, headers=world.h)
    assert r.status_code == 422 and r.json()["errors"]["field"] == "body"
    no_event = {**OFFER, "variables": {"body": [{"source": "name"}, {"source": "event_name"}], "buttons": {"0": {"source": "text", "value": "x"}}}}
    r = await client.post(url, json=no_event, headers=world.h)
    assert r.status_code == 422 and r.json()["errors"]["field"] == "body.1"
    r = await client.post(url, json={**update_for(world), "event_id": None}, headers=world.h)
    assert r.status_code == 422 and r.json()["errors"]["field"] == "event_id"
    r = await client.post(url, json={**update_for(world), "event_id": world.other_event.id}, headers=world.h)
    assert r.status_code == 404 and r.json()["error_code"] == "EVENT_NOT_FOUND"
    r = await client.post(url, json={"template": "doc_offer", "variables": {"body": [{"source": "name"}]}}, headers=world.h)
    assert r.status_code == 422 and r.json()["error_code"] == "INVALID_TEMPLATE"
    r = await client.post(url, json={**OFFER, "kind": "event_update"}, headers=world.h)
    assert r.status_code == 422 and r.json()["error_code"] == "KIND_MISMATCH"
    r = await client.post(url, json={**OFFER, "template": "old_promo"}, headers=world.h)
    assert r.status_code == 409 and r.json()["error_code"] == "TEMPLATE_NOT_APPROVED"
    blank = {**update_for(world), "variables": {"body": [{"source": "name"}, {"source": "event_name"}, {"source": "event_date"},
                                                         {"source": "text", "value": "   "}]}}
    assert (await client.post(url, json=blank, headers=world.h)).json()["errors"]["field"] == "body.3"
    # Nobody to send it to.
    r = await client.post(f"{API}/clubs/whatsapp/broadcasts", json=OFFER, headers=world.h)
    assert r.status_code == 422 and r.json()["error_code"] == "NO_RECIPIENTS"
    assert meta.club_sent == []


async def test_a_scheduled_broadcast_reads_its_audience_when_it_goes_and_can_be_cancelled(client, session, world, meta, make_user,
                                                                                          make_registration):
    await connected(client, session, world)
    at = datetime.now(timezone.utc) + timedelta(hours=2)
    r = await client.post(f"{API}/clubs/whatsapp/broadcasts", json=update_for(world, scheduled_at=at.isoformat()), headers=world.h)
    assert r.status_code == 200 and r.json()["status"] == "scheduled"
    bid = r.json()["id"]
    assert meta.club_sent == []
    await _guest(session, world, make_user, make_registration, "late@example.com", "9000000081")
    out = await broadcasts.run(session, bid, now=at + timedelta(minutes=1))
    assert out["status"] == "done" and len(meta.club_sent) == 2

    second = await client.post(f"{API}/clubs/whatsapp/broadcasts", json=update_for(world, scheduled_at=at.isoformat()), headers=world.h)
    sid = second.json()["id"]
    c = await client.post(f"{API}/clubs/whatsapp/broadcasts/{sid}/cancel", headers=world.h)
    assert c.status_code == 200 and c.json()["status"] == "cancelled"
    assert (await broadcasts.run(session, sid, now=at + timedelta(minutes=1)))["status"] == "cancelled"
    assert len(meta.club_sent) == 2
    assert (await client.post(f"{API}/clubs/whatsapp/broadcasts/{sid}/cancel", headers=world.h)).status_code == 409
    past = await client.post(f"{API}/clubs/whatsapp/broadcasts", json=update_for(world, scheduled_at=(NOW - timedelta(days=1)).isoformat()),
                             headers=world.h)
    assert past.status_code == 422 and past.json()["errors"]["field"] == "scheduled_at"


async def test_broadcasts_are_enterprise_only_and_never_an_impersonating_admins(client, session, world, meta, make_admin, monkeypatch):
    from tests.test_plan_enforcement import impersonating

    await connected(client, session, world)
    r = await client.post(f"{API}/clubs/whatsapp/broadcasts/preview", json=OFFER, headers=world.other_h)
    assert r.status_code == 403 and r.json()["error_code"] == "PLAN_FEATURE_LOCKED"
    r = await client.post(f"{API}/clubs/whatsapp/broadcasts", json=OFFER, headers=world.other_h)
    assert r.status_code == 403 and r.json()["error_code"] == "PLAN_FEATURE_LOCKED"
    h = await impersonating(session, make_admin, world)
    for method, path, body in [("GET", "/broadcasts", None), ("POST", "/broadcasts/preview", OFFER), ("POST", "/broadcasts", OFFER),
                               ("GET", "/broadcasts/1", None), ("POST", "/broadcasts/1/cancel", None)]:
        r = await client.request(method, f"{API}/clubs/whatsapp{path}", json=body, headers=h)
        assert r.status_code == 403 and r.json()["error_code"] == "WHATSAPP_PRIVATE", (method, path, r.text)
    no_permission = await member(session, world, "editor")
    r = await client.get(f"{API}/clubs/whatsapp/broadcasts", headers=_h(no_permission))
    assert r.status_code == 403 and r.json()["error_code"] == "WHATSAPP_PERMISSION"
    allowed = await member(session, world, "editor", whatsapp_permission=True)
    assert (await client.get(f"{API}/clubs/whatsapp/broadcasts", headers=_h(allowed))).status_code == 200
    assert meta.club_sent == []


async def test_the_broadcasts_people_go_with_the_inbox_retention(client, session, world, meta):
    await connected(client, session, world)
    r = await client.post(f"{API}/clubs/whatsapp/broadcasts", json=update_for(world), headers=world.h)
    bid = r.json()["id"]
    b = await session.get(ClubWhatsAppBroadcasts, bid, populate_existing=True)
    b.finished_at = NOW - timedelta(days=400)
    await session.commit()
    assert await broadcasts.forget(session, NOW) == 1
    assert await session.scalar(select(func.count()).select_from(ClubWhatsAppBroadcastRecipients)) == 0
    page = (await client.get(f"{API}/clubs/whatsapp/broadcasts/{bid}", headers=world.h)).json()
    assert page["results"]["sent"] == 1


# ─── Connect with Meta (Embedded Signup) ─────────────────────────────────


def finish(**extra) -> dict:
    return {"code": CODE, "phone_number_id": PHONE, "messaging_account_ids": [WABA], "is_on_biz_app": False,
            "business_id": "2729063490586005", "event": "FINISH", **extra}


async def test_connect_with_meta_end_to_end(client, session, world, meta, es_on):
    meta.token_info = {**meta.token_info, "expires_at": int((NOW + timedelta(days=60)).timestamp())}
    reach = (await client.get(f"{API}/clubs/whatsapp/account/reach", headers=world.h)).json()
    assert reach["embedded_signup"] == {"app_id": APP_ID, "config_id": CONFIG_ID, "graph_version": "v24.0", "version": "v5"}

    r = await client.post(f"{API}/clubs/whatsapp/account/embedded-signup", json=finish(), headers=world.h)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["connected"] is True and out["status"] == "active" and out["mode"] == "embedded"
    assert out["display_phone_number"] == "+91 98470 12345" and ES_TOKEN not in r.text and CODE not in r.text

    # 1. The code, exchanged server to server with the platform app's id and secret.
    assert meta.exchanged == [{"client_id": APP_ID, "client_secret": ES_SECRET, "code": CODE}]
    # 2. The token checked; its expiry kept.
    row = await account_of(session, world.club.id)
    assert row.mode == "embedded" and crypto.decrypt(row.access_token_enc) == ES_TOKEN and row.app_secret_enc is None
    assert row.app_id == APP_ID and row.business_id == "2729063490586005" and row.token_last4 == "Bz42"
    assert abs((row.token_expires_at - (NOW + timedelta(days=60))).total_seconds()) < 2
    # 3-4. Subscribed with the club's own webhook address and verify token.
    assert meta.subscribed == [{"override_callback_uri": accounts.webhook_url(row.webhook_token),
                                "verify_token": crypto.decrypt(row.verify_token_enc)}]
    # 5. Registered with a 6-digit PIN, kept sealed.
    assert len(meta.registered) == 1 and re.fullmatch(r"\d{6}", meta.registered[0]["pin"])
    assert meta.registered[0]["messaging_product"] == "whatsapp"
    assert crypto.decrypt(row.register_pin_enc) == meta.registered[0]["pin"] and row.registered_at is not None
    # 6. Its templates read, as for a pasted connection.
    assert await session.scalar(select(func.count()).select_from(ClubWhatsAppTemplates)) > 0
    # Every call with the business token carries the platform app's appsecret_proof.
    proof = hmac.new(ES_SECRET.encode(), ES_TOKEN.encode(), hashlib.sha256).hexdigest()
    number = meta.club_calls("GET", f"/{PHONE}")[0]
    assert parse_qs(number.url.query.decode())["appsecret_proof"] == [proof]

    # The club's webhook checks Meta's signature with the platform app's secret.
    msg = {"from": GUEST, "id": "wamid.ESIN1", "timestamp": str(int(NOW.timestamp())), "type": "text", "text": {"body": "hi"}}
    assert (await hook(client, row.webhook_token, inbound(msg), secret=SECRET)).status_code == 403
    r = await hook(client, row.webhook_token, inbound(msg), secret=ES_SECRET)
    assert r.status_code == 200 and r.json()["ok"] is True
    v = await client.get(f"{API}/hooks/whatsapp/club/{row.webhook_token}",
                         params={"hub.mode": "subscribe", "hub.verify_token": crypto.decrypt(row.verify_token_enc), "hub.challenge": "42"})
    assert v.status_code == 200 and v.text == "42"

    audit = await session.scalar(select(AdminAuditLogs).where(AdminAuditLogs.action == "club_whatsapp.connect"))
    text = json.dumps(audit.details)
    assert audit.details["mode"] == "embedded" and audit.details["registered"] is True
    assert ES_TOKEN not in text and CODE not in text and meta.registered[0]["pin"] not in text
    # And its tickets go from it, as a pasted one's do.
    assert await accounts.route_for(session, world.club.id, "event_reminder") is not None


async def test_connect_with_meta_is_off_until_its_flag_is_on(client, session, world, meta):
    reach = (await client.get(f"{API}/clubs/whatsapp/account/reach", headers=world.h)).json()
    assert reach["embedded_signup"] is None
    r = await client.post(f"{API}/clubs/whatsapp/account/embedded-signup", json=finish(), headers=world.h)
    assert r.status_code == 404 and r.json()["error_code"] == "EMBEDDED_SIGNUP_OFF"
    assert meta.calls == [] and await account_of(session, world.club.id) is None
    # Pasting keys works either way.
    assert (await connected(client, session, world)).mode == "keys"


async def test_an_expired_code_says_to_connect_again(client, session, world, meta, es_on):
    meta.exchange_answer = (400, {"error": {"message": "This authorization code has expired.", "type": "OAuthException", "code": 100,
                                            "error_subcode": 36007}})
    r = await client.post(f"{API}/clubs/whatsapp/account/embedded-signup", json=finish(), headers=world.h)
    assert r.status_code == 422 and r.json()["error_code"] == "EMBEDDED_SIGNUP_CODE_EXPIRED"
    assert await account_of(session, world.club.id) is None and meta.registered == []
    meta.exchange_answer = (400, {"error": {"message": "Invalid verification code format.", "type": "OAuthException", "code": 100}})
    r = await client.post(f"{API}/clubs/whatsapp/account/embedded-signup", json=finish(), headers=world.h)
    assert r.status_code == 422 and r.json()["error_code"] == "EMBEDDED_SIGNUP_REFUSED"


async def test_a_cancelled_or_failed_flow_is_kept_for_metas_support(client, session, world, meta, es_on):
    url = f"{API}/clubs/whatsapp/account/embedded-signup/event"
    r = await client.post(url, json={"event": "CANCEL", "current_step": "PHONE_NUMBER_SETUP"}, headers=world.h)
    assert r.status_code == 200 and r.json() == {"ok": True}
    r = await client.post(url, json={"event": "ERROR", "error_message": "Your verified name violates WhatsApp guidelines.",
                                     "error_code": "524126", "session_id": "f34b51dab5e0498"}, headers=world.h)
    assert r.status_code == 200
    rows = (await session.scalars(select(AdminAuditLogs).where(AdminAuditLogs.action == "club_whatsapp.embedded_signup")
                                  .order_by(AdminAuditLogs.id))).all()
    assert [a.details["event"] for a in rows] == ["CANCEL", "ERROR"]
    assert rows[0].details["current_step"] == "PHONE_NUMBER_SETUP" and rows[1].details["session_id"] == "f34b51dab5e0498"
    assert (await client.post(url, json={"event": "FINISH"}, headers=world.h)).status_code == 422
    assert meta.calls == [] and await account_of(session, world.club.id) is None


async def test_a_number_meta_wont_register_yet_waits_and_check_again_registers_it(client, session, world, meta, es_on):
    meta.register_answer = lambda body: httpx.Response(400, json={"error": {
        "code": 133016, "message": "Registration or Deregistration failed because there were too many attempts for this phone number"}})
    r = await client.post(f"{API}/clubs/whatsapp/account/embedded-signup", json=finish(), headers=world.h)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "failed" and r.json()["last_error_code"] == "133016" and "72 hours" in r.json()["last_error"]
    row = await account_of(session, world.club.id)
    assert row.registered_at is None and await accounts.route_for(session, world.club.id, "event_reminder") is None
    pin = crypto.decrypt(row.register_pin_enc)
    # The sweep's check of the number doesn't undo it...
    from app.api.club_whatsapp import sweep

    row.quality_checked_at = None
    await session.commit()
    await sweep.refresh_quality(session)
    assert (await account_of(session, world.club.id)).status == "failed"
    # ...registering does: "Check again".
    meta.register_answer = None
    r = await client.post(f"{API}/clubs/whatsapp/account/verify", headers=world.h)
    assert r.status_code == 200 and r.json()["status"] == "active", r.text
    row = await account_of(session, world.club.id)
    assert row.registered_at is not None and [b["pin"] for b in meta.registered] == [pin, pin]


async def test_a_whatsapp_business_app_number_is_not_registered(client, session, world, meta, es_on):
    r = await client.post(f"{API}/clubs/whatsapp/account/embedded-signup",
                          json=finish(is_on_biz_app=True, event="FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING"), headers=world.h)
    assert r.status_code == 200 and r.json()["status"] == "active"
    row = await account_of(session, world.club.id)
    assert row.on_biz_app is True and meta.registered == [] and row.register_pin_enc is None


async def test_a_sign_in_without_both_permissions_or_a_number_is_refused(client, session, world, meta, es_on):
    meta.token_info = {**meta.token_info, "scopes": ["whatsapp_business_management"]}
    r = await client.post(f"{API}/clubs/whatsapp/account/embedded-signup", json=finish(), headers=world.h)
    assert r.status_code == 422 and r.json()["error_code"] == "WHATSAPP_PERMISSION_MISSING"
    meta.token_info = {**meta.token_info, "scopes": list(graph.SCOPES),
                       "granular_scopes": [{"scope": "whatsapp_business_management", "target_ids": [WABA]}]}
    meta.waba_numbers = []
    r = await client.post(f"{API}/clubs/whatsapp/account/embedded-signup", json=finish(phone_number_id=None, messaging_account_ids=[]),
                          headers=world.h)
    assert r.status_code == 422 and r.json()["error_code"] == "EMBEDDED_SIGNUP_NO_NUMBER"
    assert await account_of(session, world.club.id) is None


# ─── The app's default webhook, routed by WhatsApp Business Account ──────


def app_sign(body: bytes, secret: str = ES_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def app_hook(client, payload: dict, *, secret: str = ES_SECRET) -> httpx.Response:
    body = json.dumps(payload).encode()
    return await client.post(f"{API}/hooks/whatsapp", content=body,
                             headers={"content-type": "application/json", "x-hub-signature-256": app_sign(body, secret)})


async def test_the_apps_default_webhook_reaches_the_club_by_its_account(client, session, world, meta, es_on, monkeypatch):
    assert (await client.post(f"{API}/clubs/whatsapp/account/embedded-signup", json=finish(), headers=world.h)).status_code == 200
    template_event = {"object": "whatsapp_business_account", "entry": [
        {"id": WABA, "time": int(NOW.timestamp()), "changes": [{"field": "message_template_status_update", "value": {
            "event": "PAUSED", "message_template_id": 1, "message_template_name": "event_news", "message_template_language": "en",
            "reason": None}}]},
        {"id": WABA, "changes": [{"field": "account_update", "value": {
            "event": "ACCOUNT_RESTRICTION", "restriction_info": [{"restriction_type": "RESTRICTED_BIZ_INITIATED_MESSAGING",
                                                                   "expiration": int((NOW + timedelta(days=2)).timestamp())}]}}]},
        {"id": "111111111111111", "changes": [{"field": "account_update", "value": {"event": "ACCOUNT_DELETED"}}]},
    ]}
    # A bad signature: refused, nothing read.
    r = await app_hook(client, template_event, secret="0" * 32)
    assert r.json() == {"ok": False, "reason": "signature"}
    t = await session.scalar(select(ClubWhatsAppTemplates).where(ClubWhatsAppTemplates.name == "event_news"))
    assert t.status == "APPROVED"

    r = await app_hook(client, template_event)
    assert r.status_code == 202 and r.json()["ok"] is True
    assert r.json()["clubs"] == {"accounts": 1, "failed": 0, "ignored": 1}
    t = await session.scalar(select(ClubWhatsAppTemplates).where(ClubWhatsAppTemplates.name == "event_news")
                             .execution_options(populate_existing=True))
    row = await account_of(session, world.club.id)
    assert t.status == "PAUSED" and row.restrictions[0]["type"] == "RESTRICTED_BIZ_INITIATED_MESSAGING"
    assert accounts.sending_restricted(row)

    # Meta subscribing the default address: the platform app's verify token answers too.
    v = await client.get(f"{API}/hooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": PLATFORM_VERIFY, "hub.challenge": "7"})
    assert v.status_code == 200 and v.text == "7"


async def test_the_apps_webhook_leaves_pasted_numbers_and_the_platforms_statuses_alone(client, session, world, meta, es_on,
                                                                                      platform_number, monkeypatch):
    from app.api.hooks import service as hooks_service

    await connected(client, session, world)  # pasted keys: its own app's webhooks, not ours
    seen = []

    async def platform(session, payload):
        seen.append(payload)
        return {"matched": 0}

    monkeypatch.setattr(hooks_service, "whatsapp", platform)
    paused = {"entry": [{"id": WABA, "changes": [{"field": "message_template_status_update", "value": {
        "event": "PAUSED", "message_template_name": "event_news", "message_template_language": "en"}}]}]}
    r = await app_hook(client, paused)
    assert r.json()["clubs"] == {"accounts": 0, "failed": 0, "ignored": 1} and seen == []
    t = await session.scalar(select(ClubWhatsAppTemplates).where(ClubWhatsAppTemplates.name == "event_news"))
    assert t.status == "APPROVED"
    # Signed with the platform's own number's secret: its statuses, as ever, and no club touched.
    monkeypatch.setattr(settings, "WHATSAPP_APP_SECRET", SECRET)
    r = await app_hook(client, paused, secret=SECRET)
    assert r.status_code == 202 and "clubs" not in r.json() and seen == [paused]


async def test_the_key_rotation_seals_the_registration_pin_too(client, session, world, meta, es_on, monkeypatch):
    from scripts import rotate_whatsapp_keys

    assert (await client.post(f"{API}/clubs/whatsapp/account/embedded-signup", json=finish(), headers=world.h)).status_code == 200
    old, new = settings.WHATSAPP_KEYS_SECRET, Fernet.generate_key().decode()
    monkeypatch.setattr(settings, "WHATSAPP_KEYS_SECRET", f"{new},{old}")
    # The token, the verify token and the PIN: no app secret is kept for a number connected with Meta.
    assert await rotate_whatsapp_keys.run(session) == {"rows": 1, "rotated": 3, "unreadable": 0}
    monkeypatch.setattr(settings, "WHATSAPP_KEYS_SECRET", new)
    row = await account_of(session, world.club.id)
    assert crypto.decrypt(row.register_pin_enc) == meta.registered[0]["pin"] and crypto.decrypt(row.access_token_enc) == ES_TOKEN
