"""A club's own WhatsApp Business number (app/api/club_whatsapp).

Meta is never reached: `meta` stands in for the Graph API -- the number, the
business account's numbers and templates, sends, media, uploads -- for the
club's calls and the platform's alike, and records what it was asked. Every
other test finds Meta unreachable (conftest). What is checked is what Meta
would be asked, and what the club, its team, the deliveries and the admins
see afterwards.

The plans are production's: club_plans' seed, then club_whatsapp's grant
(Enterprise has `whatsapp_own`).
"""
import asyncio
import hashlib
import hmac
import importlib.util
import json
import logging
import pathlib
import re
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, unquote

import httpx
import pytest
import pytest_asyncio
from alembic.migration import MigrationContext
from alembic.operations import Operations
from cryptography.fernet import Fernet
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.api.admin_alerts.models import AdminNotifications
from app.api.audit.models import AdminAuditLogs
from app.api.auth.schemas import AuthTokenData
from app.api.club_access import policy
from app.api.club_access.models import ClubMembers
from app.api.club_whatsapp import crypto, graph, inbox, live, quota, sweep, templates
from app.api.club_whatsapp import service as accounts
from app.api.club_whatsapp.models import (
    ClubWhatsAppAccounts,
    ClubWhatsAppConversations,
    ClubWhatsAppMessages,
    ClubWhatsAppQualityLog,
    ClubWhatsAppTemplates,
)
from app.api.clubs.models import ClubUsersLink
from app.api.deliveries import service as deliveries
from app.api.deliveries.models import Deliveries, DeliveryChannel, DeliveryKind, DeliveryStatus
from app.api.subscriptions import gates, service as subscriptions
from app.api.subscriptions.models import BillingCycle, Plans
from app.api.users.models import UserProfiles
from app.config import settings
from app.core import after_commit, redact, whatsapp
from app.core.auth.jwt import create_access_token
from app.core.email import email as email_mod
from tests.conftest import auth_header

API = "/api/v1"
NOW = datetime.now(timezone.utc)
LONG_AGO = datetime(2020, 1, 1, tzinfo=timezone.utc)
WABA = "102030405060708"
PHONE = "109876543210123"
APP_ID = "556677889900"
TOKEN = "EAAGm0PX4ZCpsBAclubtokenNOTREAL" + "x" * 40 + "Q7zK"
SECRET = "0123456789abcdef0123456789abcdef"
KEYS = {"waba_id": WABA, "phone_number_id": PHONE, "access_token": TOKEN, "app_secret": SECRET}
GUEST = "919876543210"
PLATFORM_PHONE = "1029384756"


# ─── Meta, faked ─────────────────────────────────────────────────────────


def _tpl(name, body, *, status="APPROVED", language="en", header=None, button=None, tid=None, reason=None):
    components = []
    if header:
        components.append(header)
    components.append({"type": "BODY", "text": body})
    if button:
        components.append({"type": "BUTTONS", "buttons": [button]})
    out = {"id": tid or f"tpl-{name}-{language}", "name": name, "language": language, "status": status, "category": "UTILITY",
           "components": components}
    if reason:
        out["rejected_reason"] = reason
    return out


TICKET_BUTTON = {"type": "URL", "text": "View ticket", "url": "https://myotherapp.in/tickets/{{1}}"}


def standard_templates():
    return [
        _tpl("ticket_confirmed", templates.STANDARD["ticket_confirmed"]["body"], header={"type": "HEADER", "format": "DOCUMENT"},
             button=TICKET_BUTTON),
        _tpl("event_reminder", templates.STANDARD["event_reminder"]["body"], button=TICKET_BUTTON),
        _tpl("event_update", "Hi {{1}}, there's news about {{2}} for you."),
        _tpl("old_promo", "Hello {{1}} and welcome.", status="REJECTED", reason="INVALID_FORMAT"),
    ]


class FakeMeta:
    """Answers as Graph would, by path. `fail["GET <path>"]` = (status, json)
    makes that call refused; `fail[...] = "network"` makes it unreachable."""

    def __init__(self):
        self.calls: list[httpx.Request] = []
        self.club_sent: list[dict] = []
        self.platform_sent: list[dict] = []
        self.created: list[dict] = []
        self.deleted: list[dict] = []
        self.number = {"id": PHONE, "display_phone_number": "+91 98470 12345", "verified_name": "Yukthi",
                       "quality_rating": "GREEN", "messaging_limit_tier": "TIER_1K"}
        self.waba_numbers = [PHONE]
        self.templates = standard_templates()
        self.fail: dict = {}
        self.club_answer = None
        self.media_bytes = b"\xff\xd8\xff\xe0fake-jpeg"
        #: Overrides of what GET /{media id} answers (its url, mime_type, file_size).
        self.media_info: dict = {}
        #: (status, body) the media host answers first, one per download.
        self.download_answers: list = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.calls.append(request)
        if request.url.host != "graph.facebook.com":
            if self.download_answers:
                status, body = self.download_answers.pop(0)
                return httpx.Response(status, content=body if isinstance(body, bytes) else json.dumps(body).encode())
            return httpx.Response(200, content=self.media_bytes, headers={"content-type": "image/jpeg"})
        path = re.sub(r"^/v\d+\.\d+/", "", request.url.path)
        key = f"{request.method} {path}"
        if key in self.fail:
            answer = self.fail[key]
            if answer == "network":
                raise httpx.ConnectError("unreachable", request=request)
            return httpx.Response(answer[0], json=answer[1])
        if path == PLATFORM_PHONE + "/messages":
            body = json.loads(request.content)
            self.platform_sent.append(body)
            return httpx.Response(200, json={"messages": [{"id": f"wamid.PLATFORM{len(self.platform_sent)}"}]})
        if path == PLATFORM_PHONE + "/media" or path == PHONE + "/media":
            return httpx.Response(200, json={"id": "media-up-1"})
        if request.method == "GET" and (path == PHONE or path in self.waba_numbers):
            return httpx.Response(200, json={**self.number, "id": path})
        if path == f"{WABA}/phone_numbers":
            return httpx.Response(200, json={"data": [{"id": n} for n in self.waba_numbers]})
        if path == f"{WABA}/subscribed_apps":
            return httpx.Response(200, json={"success": True})
        if path == f"{WABA}/message_templates":
            if request.method == "GET":
                return httpx.Response(200, json={"data": self.templates, "paging": {"cursors": {}}})
            if request.method == "POST":
                body = json.loads(request.content)
                self.created.append(body)
                return httpx.Response(200, json={"id": f"tpl-new-{len(self.created)}", "status": "PENDING", "category": body["category"]})
            self.deleted.append(dict(request.url.params))
            return httpx.Response(200, json={"success": True})
        if path == PHONE + "/messages":
            body = json.loads(request.content)
            if body.get("status") == "read":
                return httpx.Response(200, json={"success": True})
            self.club_sent.append(body)
            if self.club_answer is not None:
                return self.club_answer(body)
            return httpx.Response(200, json={"messages": [{"id": f"wamid.CLUB{len(self.club_sent)}"}]})
        if path.startswith("media-") and request.method == "GET":
            return httpx.Response(200, json={"url": "https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=1",
                                             "mime_type": "image/jpeg", "file_size": len(self.media_bytes), "id": path,
                                             **self.media_info})
        if path == f"{APP_ID}/uploads":
            return httpx.Response(200, json={"id": "upload:MTphdHRhY2htZW50"})
        if path == "upload:MTphdHRhY2htZW50":
            return httpx.Response(200, json={"h": "4::aGFuZGxl"})
        return httpx.Response(404, json={"error": {"message": f"unknown path {key}", "code": 100}})

    def club_calls(self, method: str, suffix: str) -> list[httpx.Request]:
        return [r for r in self.calls if r.method == method and r.url.path.endswith(suffix)]


@pytest.fixture
def meta(monkeypatch):
    fake = FakeMeta()
    monkeypatch.setattr(settings, "WHATSAPP_KEYS_SECRET", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "WHATSAPP_GRAPH_VERSION", "v23.0", raising=False)
    monkeypatch.setattr(graph, "_client", lambda timeout=None: httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)))
    monkeypatch.setattr(whatsapp, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)))
    return fake


@pytest.fixture
def platform_number(monkeypatch):
    """The platform's own WhatsApp number configured too."""
    monkeypatch.setattr(settings, "WHATSAPP_ACCESS_TOKEN", "platform-token", raising=False)
    monkeypatch.setattr(settings, "WHATSAPP_PHONE_NUMBER_ID", PLATFORM_PHONE, raising=False)


@pytest.fixture
def mails(monkeypatch):
    sent: list[dict] = []

    def send(**kwargs):
        sent.append(kwargs)
        return {"MessageId": f"ses-{len(sent)}"}

    monkeypatch.setattr(email_mod, "send_email", send)
    return sent


@pytest.fixture(autouse=True)
def _enforced(monkeypatch):
    monkeypatch.setattr(settings, "PLAN_ENFORCEMENT", True)
    monkeypatch.setattr(settings, "PLAN_ENFORCEMENT_FROM", LONG_AGO)
    monkeypatch.setattr(settings, "LIVE_SSE", False)
    gates.reset()
    yield
    gates.reset()


def _migration(name):
    path = pathlib.Path(__file__).resolve().parents[1] / "migrations" / "versions" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest_asyncio.fixture
async def plans(session):
    club_plans, mine = _migration("club_plans"), _migration("club_whatsapp")
    connection = await session.connection()

    def seed(sync):
        with Operations.context(MigrationContext.configure(connection=sync)):
            club_plans.seed_plans()
            mine.grant()

    await connection.run_sync(seed)
    rows = (await session.execute(select(Plans).options(selectinload(Plans.limits), selectinload(Plans.features)))).scalars().all()
    return {p.key: p for p in rows}


@pytest_asyncio.fixture
async def world(session, make_club, make_event, make_user, make_registration, mint_token, plans):
    owner, club = await make_club(name="Yukthi", email="yukthi@example.com")
    other_owner, other_club = await make_club(name="Robotics Guild", email="robotics@example.com")
    for c in (club, other_club):
        await subscriptions.assign_plan(session, club_id=c.id, plan=plans["enterprise" if c is club else "free"], cycle=BillingCycle.monthly)
    event = await make_event(club=club, created_by=owner, name="Build Night",
                             event_datetime=NOW + timedelta(days=3), location_name="Innovation Hall")
    other_event = await make_event(club=other_club, created_by=other_owner, name="Their Night", event_datetime=NOW + timedelta(days=5))
    guest = await make_user(email="ananya@example.com")
    reg = await make_registration(event=event, user=guest, full_name="Ananya Iyer", email=guest.email, phone="98765 43210",
                                  whatsapp_opt_in=True, whatsapp_opt_in_at=NOW)
    stranger = await make_user(email="bala@example.com")
    other_reg = await make_registration(event=other_event, user=stranger, full_name="Bala Krishnan", email=stranger.email,
                                        phone="9123456780", whatsapp_opt_in=True)
    await session.commit()
    return SimpleNamespace(
        owner=owner, club=club, other_owner=other_owner, other_club=other_club, event=event, other_event=other_event,
        guest=guest, reg=reg, stranger=stranger, other_reg=other_reg, plans=plans,
        h=auth_header(await mint_token(owner)), other_h=auth_header(await mint_token(other_owner)),
    )


async def member(session, w, role, *, whatsapp_permission=False) -> dict:
    row = ClubMembers(club_id=w.club.id, email=f"{role}-{uuid.uuid4().hex[:6]}@example.com", full_name=f"Pat {role.title()}",
                      role=role, status="active", whatsapp=whatsapp_permission)
    session.add(row)
    await session.commit()
    await session.refresh(w.owner)
    token = create_access_token(
        data=AuthTokenData(user_id=w.owner.id, token_type="access_token", token_version=w.owner.token_version, member_id=row.id).model_dump()
    )
    return {**auth_header(token), "_member_id": str(row.id)}


def _h(headers: dict) -> dict:
    return {k: v for k, v in headers.items() if not k.startswith("_")}


async def connect(client, w, **override) -> httpx.Response:
    return await client.put(f"{API}/clubs/whatsapp/account", json={**KEYS, **override}, headers=w.h)


async def account_of(session, club_id) -> ClubWhatsAppAccounts:
    return await session.scalar(
        select(ClubWhatsAppAccounts).where(ClubWhatsAppAccounts.club_id == club_id).order_by(ClubWhatsAppAccounts.id.desc())
        .execution_options(populate_existing=True)
    )


def sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def hook(client, token: str, payload: dict, *, secret: str = SECRET) -> httpx.Response:
    body = json.dumps(payload).encode()
    return await client.post(f"{API}/hooks/whatsapp/club/{token}", content=body,
                             headers={"content-type": "application/json", "x-hub-signature-256": sign(body, secret)})


def text_msg(wamid: str, body: str, *, wa: str = GUEST, at: datetime | None = None, kind: str = "text", part: dict | None = None) -> dict:
    out = {"from": wa, "id": wamid, "timestamp": str(int((at or datetime.now(timezone.utc)).timestamp())), "type": kind}
    out[kind] = part if part is not None else {"body": body}
    return out


def inbound(*messages, name: str = "Ananya", wa: str = GUEST, phone_id: str = PHONE, statuses=None) -> dict:
    value = {"messaging_product": "whatsapp", "metadata": {"display_phone_number": "919847012345", "phone_number_id": phone_id},
             "contacts": [{"profile": {"name": name}, "wa_id": wa}]}
    if messages:
        value["messages"] = list(messages)
    if statuses:
        value["statuses"] = statuses
    return {"object": "whatsapp_business_account", "entry": [{"id": WABA, "changes": [{"field": "messages", "value": value}]}]}


async def connected(client, session, w) -> ClubWhatsAppAccounts:
    r = await connect(client, w)
    assert r.status_code == 200, r.text
    return await account_of(session, w.club.id)


async def conversation_with(client, session, w, account, *, at: datetime | None = None, body: str = "Is parking free?") -> ClubWhatsAppConversations:
    r = await hook(client, account.webhook_token, inbound(text_msg(f"wamid.IN{uuid.uuid4().hex[:8]}", body, at=at)))
    assert r.json()["ok"] is True, r.text
    return await session.scalar(
        select(ClubWhatsAppConversations).where(ClubWhatsAppConversations.club_id == w.club.id, ClubWhatsAppConversations.wa_id == GUEST)
        .execution_options(populate_existing=True)
    )


# ─── Connecting: checked with Meta, kept encrypted ───────────────────────
#
# First in the module: run alone, a module whose first test is not async
# leaves its client tests on another event loop than the suite's session.


async def test_connecting_checks_the_number_with_meta_and_keeps_the_keys_encrypted(client, session, world, meta):
    r = await connect(client, world, app_id=APP_ID)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["connected"] is True and out["status"] == "active" and out["plan_allows"] is True
    assert out["display_phone_number"] == "+91 98470 12345" and out["verified_name"] == "Yukthi"
    assert out["quality_rating"] == "GREEN" and out["messaging_limit_tier"] == "TIER_1K"
    assert out["token_last4"] == "Q7zK" and out["app_id"] == APP_ID
    assert out["webhook_url"].startswith("https://api.myotherapp.in/api/v1/hooks/whatsapp/club/")
    assert out["verify_token"] and out["webhook_fields"][0] == "messages"
    # Neither the token nor the app secret is ever handed back.
    assert TOKEN not in r.text and SECRET not in r.text

    # Meta was asked with the club's token, its appsecret_proof, and nothing in the clear.
    number = meta.club_calls("GET", f"/{PHONE}")[0]
    assert number.headers["authorization"] == f"Bearer {TOKEN}"
    proof = hmac.new(SECRET.encode(), TOKEN.encode(), hashlib.sha256).hexdigest()
    assert parse_qs(number.url.query.decode())["appsecret_proof"] == [proof]
    assert "quality_rating" in parse_qs(number.url.query.decode())["fields"][0]
    assert meta.club_calls("GET", f"/{WABA}/phone_numbers") and meta.club_calls("POST", f"/{WABA}/subscribed_apps")

    row = await account_of(session, world.club.id)
    assert TOKEN not in row.access_token_enc and SECRET not in row.app_secret_enc
    assert crypto.decrypt(row.access_token_enc) == TOKEN and crypto.decrypt(row.app_secret_enc) == SECRET
    assert crypto.decrypt(row.verify_token_enc) == out["verify_token"]
    # What the account has approved is kept, for its tickets.
    names = {t.name: t.status for t in (await session.scalars(select(ClubWhatsAppTemplates))).all()}
    assert names["ticket_confirmed"] == "APPROVED" and names["old_promo"] == "REJECTED"

    audit = (await session.scalars(select(AdminAuditLogs).where(AdminAuditLogs.action == "club_whatsapp.connect"))).all()
    assert len(audit) == 1 and audit[0].details["token_last4"] == "Q7zK"
    assert TOKEN not in json.dumps(audit[0].details) and SECRET not in json.dumps(audit[0].details)
    assert await session.scalar(select(func.count()).select_from(AdminNotifications).where(AdminNotifications.kind == "club_whatsapp_connected")) == 1


def test_the_keyring_is_its_own_secret_and_rotates(monkeypatch):
    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    monkeypatch.setattr(settings, "WHATSAPP_KEYS_SECRET", old)
    monkeypatch.setattr(settings, "PAYMENT_KEYS_SECRET", "")
    sealed = crypto.encrypt("EAAtoken")
    assert sealed != "EAAtoken" and crypto.decrypt(sealed) == "EAAtoken"
    # A rotation: the new key encrypts, the old still opens what it sealed.
    monkeypatch.setattr(settings, "WHATSAPP_KEYS_SECRET", f"{new},{old}")
    assert crypto.decrypt(sealed) == "EAAtoken"
    # Payment keys' secret opens nothing here, nor the other way round.
    monkeypatch.setattr(settings, "WHATSAPP_KEYS_SECRET", "")
    monkeypatch.setattr(settings, "PAYMENT_KEYS_SECRET", old)
    assert not crypto.available()
    with pytest.raises(crypto.Unavailable):
        crypto.decrypt(sealed)


@pytest.mark.parametrize("status, error, code, field", [
    (401, {"code": 190, "message": "Invalid OAuth access token - Cannot parse access token"}, "WHATSAPP_TOKEN_INVALID", "access_token"),
    (403, {"code": 10, "message": "Application does not have permission for this action"}, "WHATSAPP_PERMISSION_MISSING", "access_token"),
    (403, {"code": 200, "message": "Permissions error"}, "WHATSAPP_PERMISSION_MISSING", "access_token"),
    (400, {"code": 100, "error_subcode": 33, "message": f"Unsupported get request. Object with ID '{PHONE}' does not exist"},
     "WHATSAPP_NUMBER_NOT_FOUND", "phone_number_id"),
    (400, {"code": 100, "message": "Invalid appsecret_proof provided in the API argument"}, "WHATSAPP_APP_SECRET_WRONG", "app_secret"),
    (400, {"code": 368, "message": "Temporarily blocked for policies violations"}, "WHATSAPP_ACCOUNT_BLOCKED", None),
    (500, {"code": 2, "message": "Service temporarily unavailable"}, "META_UNREACHABLE", None),
    (429, {"code": 80007, "message": "Rate limit hit"}, "META_UNREACHABLE", None),
])
async def test_each_refusal_from_meta_is_said_in_words_and_nothing_is_kept(client, session, world, meta, status, error, code, field):
    meta.fail[f"GET {PHONE}"] = (status, {"error": error})
    r = await connect(client, world)
    assert r.status_code == (502 if code == "META_UNREACHABLE" else 422), r.text
    body = r.json()
    assert body["error_code"] == code and body["message"]
    if code != "META_UNREACHABLE":
        assert body["errors"]["meta_code"] == str(error["code"])
    assert (body.get("errors") or {}).get("field") == field
    assert await account_of(session, world.club.id) is None


async def test_meta_unreachable_and_a_number_outside_the_account_are_refused(client, session, world, meta):
    meta.fail[f"GET {PHONE}"] = "network"
    r = await connect(client, world)
    assert r.status_code == 502 and r.json()["error_code"] == "META_UNREACHABLE"
    del meta.fail[f"GET {PHONE}"]
    meta.waba_numbers = ["100000000000001"]
    r = await connect(client, world)
    assert r.status_code == 422 and r.json()["error_code"] == "WHATSAPP_NUMBER_NOT_IN_WABA"
    assert await account_of(session, world.club.id) is None


@pytest.mark.parametrize("field, value", [
    ("waba_id", "WABA-abc"), ("phone_number_id", "+91 98470 12345"), ("access_token", "short"), ("app_secret", "not-a-secret"),
    ("app_id", "app"),
])
async def test_keys_that_cannot_be_right_are_refused_before_meta_is_asked(client, session, world, meta, field, value):
    r = await connect(client, world, **{field: value})
    assert r.status_code == 422 and r.json()["error_code"] == "INVALID_WHATSAPP_KEYS"
    assert r.json()["errors"]["field"] == field
    assert meta.calls == []


async def test_the_graph_calls_time_out(meta, monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr(settings, "WHATSAPP_TIMEOUT_SECONDS", 12.0)
    client = graph._client()
    assert client.timeout.read == 12.0 and client.timeout.connect == 5.0
    await client.aclose()


async def test_without_the_keys_secret_nothing_can_be_connected(client, session, world, meta, monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_KEYS_SECRET", "")
    r = await connect(client, world)
    assert r.status_code == 503 and r.json()["error_code"] == "WHATSAPP_UNAVAILABLE"
    assert (await client.get(f"{API}/clubs/whatsapp/account", headers=world.h)).json()["available"] is False


async def test_a_number_connected_to_another_club_is_refused(client, session, world, meta, plans):
    assert (await connect(client, world)).status_code == 200
    await subscriptions.assign_plan(session, club_id=world.other_club.id, plan=plans["enterprise"], cycle=BillingCycle.monthly)
    await session.commit()
    gates.reset()
    r = await client.put(f"{API}/clubs/whatsapp/account", json=KEYS, headers=world.other_h)
    assert r.status_code == 409 and r.json()["error_code"] == "WHATSAPP_NUMBER_IN_USE"


async def test_new_keys_keep_the_webhook_address_and_are_audited(client, session, world, meta):
    first = (await connect(client, world)).json()
    new_token = "EAAGnewtokenNOTREAL" + "y" * 40 + "Zz99"
    second = (await connect(client, world, access_token=new_token)).json()
    assert second["webhook_url"] == first["webhook_url"] and second["verify_token"] == first["verify_token"]
    assert second["token_last4"] == "Zz99"
    rows = (await session.scalars(select(ClubWhatsAppAccounts).where(ClubWhatsAppAccounts.club_id == world.club.id))).all()
    assert len(rows) == 1 and crypto.decrypt(rows[0].access_token_enc) == new_token
    actions = [a.action for a in (await session.scalars(select(AdminAuditLogs).where(AdminAuditLogs.action.like("club_whatsapp.%")))).all()]
    assert "club_whatsapp.keys_changed" in actions


async def test_disconnecting_wipes_the_keys(client, session, world, meta):
    account = await connected(client, session, world)
    token = account.webhook_token
    r = await client.delete(f"{API}/clubs/whatsapp/account", headers=world.h)
    assert r.status_code == 200 and r.json()["connected"] is False and r.json()["status"] == "disconnected"
    assert r.json()["webhook_url"] is None and r.json()["verify_token"] is None
    row = await account_of(session, world.club.id)
    assert row.access_token_enc is None and row.app_secret_enc is None and row.verify_token_enc is None and row.token_last4 is None
    assert await session.scalar(select(func.count()).select_from(ClubWhatsAppTemplates)) == 0
    assert await session.scalar(select(func.count()).select_from(AdminAuditLogs).where(AdminAuditLogs.action == "club_whatsapp.disconnect")) == 1
    # Meta's webhook for the old connection is refused: no secret to check it with.
    assert (await hook(client, token, inbound(text_msg("wamid.LATE", "hello")))).json() == {"ok": False, "reason": "unknown"}
    # And it is gone from routing.
    assert await accounts.route_for(session, world.club.id, "event_reminder") is None


# ─── The plan ────────────────────────────────────────────────────────────


async def test_only_enterprise_may_connect_and_the_page_says_what_to_upgrade_to(client, session, world, meta):
    r = await client.put(f"{API}/clubs/whatsapp/account", json=KEYS, headers=world.other_h)
    assert r.status_code == 403
    body = r.json()
    assert body["error_code"] == "PLAN_FEATURE_LOCKED" and body["errors"]["feature"] == "whatsapp_own"
    assert body["errors"]["required_plan"] == "enterprise"
    assert meta.calls == []
    page = (await client.get(f"{API}/clubs/whatsapp/account", headers=world.other_h)).json()
    assert page["plan_allows"] is False and page["upgrade"]["required_plan"] == "enterprise" and page["connected"] is False


async def test_the_flag_is_on_enterprise_alone(world):
    flags = {key: plan.features.whatsapp_own for key, plan in world.plans.items()}
    assert flags["enterprise"] is True and not any(v for k, v in flags.items() if k != "enterprise")
    assert gates.feature_message("whatsapp_own", None) == "Your own WhatsApp Business number is not included in your plan."


async def test_a_club_that_leaves_enterprise_sends_from_the_platform_again(client, session, world, meta):
    await connected(client, session, world)
    assert await accounts.route_for(session, world.club.id, "event_reminder") is not None
    await subscriptions.assign_plan(session, club_id=world.club.id, plan=world.plans["free"], cycle=BillingCycle.monthly)
    await session.commit()
    gates.reset()
    assert await accounts.route_for(session, world.club.id, "event_reminder") is None
    conv_id = (await conversation_with(client, session, world, await account_of(session, world.club.id))).id
    r = await client.post(f"{API}/clubs/whatsapp/conversations/{conv_id}/messages", json={"type": "text", "text": "Hi"}, headers=world.h)
    assert r.status_code == 403 and r.json()["error_code"] == "PLAN_FEATURE_LOCKED"


# ─── The webhook ─────────────────────────────────────────────────────────


async def test_meta_subscribes_with_the_clubs_own_verify_token(client, session, world, meta):
    account = await connected(client, session, world)
    verify = crypto.decrypt(account.verify_token_enc)
    url = f"{API}/hooks/whatsapp/club/{account.webhook_token}"
    ok = await client.get(url, params={"hub.mode": "subscribe", "hub.verify_token": verify, "hub.challenge": "1158201444"})
    assert ok.status_code == 200 and ok.text == "1158201444"
    for params in ({"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "1"},
                   {"hub.mode": "unsubscribe", "hub.verify_token": verify, "hub.challenge": "1"}):
        assert (await client.get(url, params=params)).status_code == 403
    assert (await client.get(f"{API}/hooks/whatsapp/club/not-a-real-token-at-all", params={
        "hub.mode": "subscribe", "hub.verify_token": verify, "hub.challenge": "1"})).status_code == 403


async def test_a_delivery_without_the_clubs_signature_is_refused_before_it_is_read(client, session, world, meta):
    account = await connected(client, session, world)
    r = await hook(client, account.webhook_token, inbound(text_msg("wamid.FORGED", "hello")), secret="f" * 32)
    # 403: Meta delivers it again while the club puts its app secret right.
    assert r.status_code == 403 and r.json() == {"ok": False, "reason": "signature"}
    unsigned = await client.post(f"{API}/hooks/whatsapp/club/{account.webhook_token}", json=inbound(text_msg("wamid.X", "hi")))
    assert unsigned.status_code == 403 and unsigned.json()["reason"] == "signature"
    # A header beyond ASCII is just not the signature (it was a 500).
    odd = await client.post(f"{API}/hooks/whatsapp/club/{account.webhook_token}", content=b"{}",
                            headers={"content-type": "application/json", "x-hub-signature-256": "sha256=ü".encode()})
    assert odd.status_code == 403
    assert await session.scalar(select(func.count()).select_from(ClubWhatsAppMessages)) == 0
    row = await account_of(session, world.club.id)
    assert "signature" in row.last_error
    # The admins hear of it once an hour, however often Meta tries.
    assert await session.scalar(select(func.count()).select_from(AdminNotifications).where(AdminNotifications.kind == "club_whatsapp_failed")) == 1


async def test_a_bad_signature_does_not_hide_what_the_page_already_says(client, session, world, meta):
    account = await connected(client, session, world)
    account.last_error = "Meta restricted the WhatsApp account (ACCOUNT_RESTRICTION)."
    await session.commit()
    r = await hook(client, account.webhook_token, inbound(text_msg("wamid.FORGED", "hello")), secret="f" * 32)
    assert r.status_code == 403
    assert (await account_of(session, world.club.id)).last_error == "Meta restricted the WhatsApp account (ACCOUNT_RESTRICTION)."


async def test_the_verify_token_is_compared_as_bytes(client, session, world, meta):
    account = await connected(client, session, world)
    r = await client.get(f"{API}/hooks/whatsapp/club/{account.webhook_token}",
                         params={"hub.mode": "subscribe", "hub.verify_token": "വെരിഫൈ", "hub.challenge": "1"})
    assert r.status_code == 403


async def test_unreadable_keys_answer_503_and_the_admins_hear_once_an_hour(client, session, world, meta, monkeypatch):
    account = await connected(client, session, world)
    monkeypatch.setattr(settings, "WHATSAPP_KEYS_SECRET", Fernet.generate_key().decode())
    for n in range(2):
        r = await hook(client, account.webhook_token, inbound(text_msg(f"wamid.K{n}", "hello")))
        assert r.status_code == 503 and r.json()["reason"] == "keys"
    told = (await session.scalars(select(AdminNotifications).where(AdminNotifications.kind == "club_whatsapp_failed"))).all()
    assert len(told) == 1 and told[0].data["code"] == "keys_unreadable"
    # Not the club's doing: its number is not marked, nor the club mailed.
    assert (await account_of(session, world.club.id)).status == "active"


async def test_a_body_larger_than_meta_sends_is_refused_unread(client, session, world, meta):
    account = await connected(client, session, world)
    body = b"{" + b" " * (4 * 1024 * 1024) + b"}"
    r = await client.post(f"{API}/hooks/whatsapp/club/{account.webhook_token}", content=body,
                          headers={"content-type": "application/json", "x-hub-signature-256": sign(body)})
    assert r.status_code == 413
    # Before the token is looked up at all.
    r = await client.post(f"{API}/hooks/whatsapp/club/not-a-club-token-xyz", content=body, headers={"content-type": "application/json"})
    assert r.status_code == 413
    assert (await account_of(session, world.club.id)).last_webhook_at is None


async def test_one_message_that_fails_is_undone_alone_and_comes_again(client, session, world, meta, monkeypatch):
    """Each message in its own SAVEPOINT: the other is kept, Meta is told
    503, and when it delivers the batch again nothing is kept twice."""
    account = await connected(client, session, world)
    first, second = text_msg("wamid.ONE", "first"), text_msg("wamid.TWO", "second")
    real_parse = inbox.parse_inbound
    broken = {"wamid.TWO"}

    def parse(m):
        if m.get("id") in broken:
            raise RuntimeError("not today")
        return real_parse(m)

    monkeypatch.setattr(inbox, "parse_inbound", parse)
    r = await hook(client, account.webhook_token, inbound(first, second))
    assert r.status_code == 503 and r.json()["messages"] == 1 and r.json()["failed"] == 1
    assert (await session.scalars(select(ClubWhatsAppMessages.wamid))).all() == ["wamid.ONE"]
    broken.clear()
    r = await hook(client, account.webhook_token, inbound(first, second))
    assert r.status_code == 200 and r.json() == {"ok": True, "messages": 1, "statuses": 0, "failed": 0}
    assert sorted((await session.scalars(select(ClubWhatsAppMessages.wamid))).all()) == ["wamid.ONE", "wamid.TWO"]
    conv = await session.scalar(select(ClubWhatsAppConversations).execution_options(populate_existing=True))
    assert conv.unread_count == 2


async def test_a_clubs_webhook_cannot_touch_another_clubs_ticket(client, session, world, meta, platform_number, make_user, make_registration):
    """A club signs its own webhook, so whatever it sends there is only
    believed about its own messages: a status naming another club's ticket
    changes nothing."""
    account = await connected(client, session, world)
    other = await make_registration(event=world.other_event, user=await make_user(email="t2@example.com"), full_name="Tara", email="t2@example.com",
                                    phone="9000000009", whatsapp_opt_in=True, created_at=NOW - timedelta(days=10))
    await session.commit()
    await deliveries.queue(session, world.other_event, DeliveryKind.reminder, registration_ids=[other.id])
    row = await session.scalar(select(Deliveries).where(Deliveries.registration_id == other.id, Deliveries.channel == DeliveryChannel.whatsapp))
    d = await deliveries.deliver(session, row.id)
    assert d.provider_id == "wamid.PLATFORM1"
    st = {"id": "wamid.PLATFORM1", "status": "failed", "timestamp": str(int(NOW.timestamp())), "recipient_id": "919000000009",
          "errors": [{"code": 131026, "title": "Message undeliverable"}]}
    r = await hook(client, account.webhook_token, inbound(statuses=[st]))
    assert r.status_code == 200
    d = await session.get(Deliveries, d.id, populate_existing=True)
    await session.refresh(other)
    assert d.outcome is None and d.status == DeliveryStatus.sent and other.wa_bounced_at is None


async def test_meta_failing_a_ticket_for_the_clubs_account_sends_it_again_from_the_platform(
        client, session, world, meta, platform_number, mails):
    await connected(client, session, world)
    d = await _reminder(session, world)
    assert d.provider_id == "wamid.CLUB1"
    account = await account_of(session, world.club.id)
    st = {"id": "wamid.CLUB1", "status": "failed", "timestamp": str(int(NOW.timestamp())), "recipient_id": GUEST,
          "errors": [{"code": 131042, "title": "Business eligibility payment issue",
                      "error_data": {"details": "Message failed to send because there were one or more errors related to your payment method."}}]}
    for _ in range(2):  # and Meta may say it twice
        r = await hook(client, account.webhook_token, inbound(statuses=[st]))
        assert r.status_code == 200, r.text
    account = await account_of(session, world.club.id)
    assert account.status == "failed" and account.last_error_code == "131042"
    assert await session.scalar(select(func.count()).select_from(AdminNotifications).where(AdminNotifications.kind == "club_whatsapp_failed")) == 1
    d = await session.get(Deliveries, d.id, populate_existing=True)
    assert d.status == DeliveryStatus.pending and d.provider_id is None and "MyOtherApp's number" in d.error
    await session.refresh(world.reg)
    assert world.reg.wa_bounced_at is None
    d = await deliveries.deliver(session, d.id)
    assert d.status == DeliveryStatus.sent and d.provider_id == "wamid.PLATFORM1"
    msg = await session.scalar(select(ClubWhatsAppMessages).where(ClubWhatsAppMessages.wamid == "wamid.CLUB1")
                               .execution_options(populate_existing=True))
    assert msg.status == "failed" and msg.error_code == "131042"


async def test_a_message_is_kept_once_opens_the_window_and_is_linked_to_the_registrant(client, session, world, meta):
    account = await connected(client, session, world)
    msg = text_msg("wamid.HBgM1", "Is there parking at the venue?")
    for _ in range(2):  # Meta may deliver it twice
        r = await hook(client, account.webhook_token, inbound(msg))
        assert r.status_code == 200 and r.json()["ok"] is True
    rows = (await session.scalars(select(ClubWhatsAppMessages))).all()
    assert len(rows) == 1 and rows[0].body == "Is there parking at the venue?" and rows[0].direction == "in"
    conv = await session.get(ClubWhatsAppConversations, rows[0].conversation_id, populate_existing=True)
    assert conv.wa_id == GUEST and conv.profile_name == "Ananya" and conv.unread_count == 1
    assert conv.registration_id == world.reg.id and conv.user_id == world.guest.id and conv.contact_name == "Ananya Iyer"
    assert conv.awaiting_since is not None and inbox.window_open(conv)
    assert (await account_of(session, world.club.id)).last_webhook_at is not None

    listed = (await client.get(f"{API}/clubs/whatsapp/conversations", headers=world.h)).json()
    item = listed["items"][0]
    assert listed["unread_total"] == 1 and item["phone"] == "+919876543210" and item["window_open"] is True
    assert item["contact"]["name"] == "Ananya Iyer" and [e["name"] for e in item["contact"]["events"]] == ["Build Night"]
    assert item["last_message"]["preview"] == "Is there parking at the venue?" and item["awaiting_reply"] is True


async def test_a_number_known_only_to_another_club_is_not_named_here(client, session, world, meta, make_user, make_registration):
    account = await connected(client, session, world)
    # Bala registered with Robotics Guild only: Yukthi learns nothing about him.
    await hook(client, account.webhook_token, inbound(text_msg("wamid.BALA", "hello?", wa="919123456780"), name="B", wa="919123456780"))
    conv = await session.scalar(select(ClubWhatsAppConversations).where(ClubWhatsAppConversations.wa_id == "919123456780"))
    assert conv.user_id is None and conv.registration_id is None and conv.contact_name is None
    # A follower whose profile has the number is not named either: the club never
    # sees that number, and it signs its own webhook, so it could ask about any.
    fan = await make_user(email="fan@example.com")
    session.add_all([UserProfiles(user_id=fan.id, full_name="Chitra Nair", whatsapp="+91 99887 76655"),
                     ClubUsersLink(club_id=world.club.id, user_id=fan.id, is_following=True)])
    await session.commit()
    await hook(client, account.webhook_token, inbound(text_msg("wamid.FAN", "hi", wa="919988776655"), name="C", wa="919988776655"))
    conv = await session.scalar(select(ClubWhatsAppConversations).where(ClubWhatsAppConversations.wa_id == "919988776655"))
    assert conv.user_id is None and conv.contact_name is None and conv.linked_at is None
    # Nor a registration that was deleted.
    gone_user = await make_user(email="gone@example.com")
    gone = await make_registration(event=world.event, user=gone_user, full_name="Deleted Dev", email=gone_user.email, phone="9111111111",
                                   whatsapp_opt_in=True)
    gone.is_deleted = True
    await session.commit()
    await hook(client, account.webhook_token, inbound(text_msg("wamid.GONE", "hi", wa="919111111111"), name="D", wa="919111111111"))
    conv = await session.scalar(select(ClubWhatsAppConversations).where(ClubWhatsAppConversations.wa_id == "919111111111"))
    assert conv.registration_id is None and conv.contact_name is None
    listed = (await client.get(f"{API}/clubs/whatsapp/conversations", params={"q": "91111"}, headers=world.h)).json()["items"][0]
    assert listed["contact"]["events"] == []
    r = await client.post(f"{API}/clubs/whatsapp/conversations", json={"registration_id": str(gone.id)}, headers=world.h)
    assert r.status_code == 404


async def test_every_kind_of_message_is_kept_as_what_it_is(client, session, world, meta):
    account = await connected(client, session, world)
    messages = [
        text_msg("wamid.IMG", "", kind="image", part={"id": "media-77", "mime_type": "image/jpeg", "caption": "The poster"}),
        text_msg("wamid.DOC", "", kind="document", part={"id": "media-78", "mime_type": "application/pdf", "filename": "id.pdf"}),
        text_msg("wamid.LOC", "", kind="location", part={"latitude": 8.5, "longitude": 76.9, "name": "CET"}),
        text_msg("wamid.REACT", "", kind="reaction", part={"message_id": "wamid.IMG", "emoji": "👍"}),
        text_msg("wamid.BTN", "", kind="button", part={"payload": "yes", "text": "I'm coming"}),
        text_msg("wamid.INT", "", kind="interactive", part={"type": "button_reply", "button_reply": {"id": "b1", "title": "Directions"}}),
        text_msg("wamid.ODD", "", kind="ephemeral", part={}),
    ]
    await hook(client, account.webhook_token, inbound(*messages))
    rows = {m.wamid: m for m in (await session.scalars(select(ClubWhatsAppMessages))).all()}
    assert rows["wamid.IMG"].type == "image" and rows["wamid.IMG"].media_id == "media-77" and rows["wamid.IMG"].body == "The poster"
    assert rows["wamid.DOC"].media_filename == "id.pdf" and rows["wamid.DOC"].media_mime == "application/pdf"
    assert rows["wamid.LOC"].location == {"latitude": 8.5, "longitude": 76.9, "name": "CET"}
    assert rows["wamid.REACT"].body == "👍" and rows["wamid.REACT"].context_wamid == "wamid.IMG"
    assert rows["wamid.BTN"].body == "I'm coming" and rows["wamid.INT"].body == "Directions"
    assert rows["wamid.ODD"].type == "unsupported"
    conv = await session.scalar(select(ClubWhatsAppConversations).execution_options(populate_existing=True))
    thread = (await client.get(f"{API}/clubs/whatsapp/conversations/{conv.id}/messages", headers=world.h)).json()
    image = next(m for m in thread["items"] if m["wamid"] == "wamid.IMG")
    # Never Meta's address: the inbox's own, behind the session.
    assert image["media"]["url"] == f"/api/v1/clubs/whatsapp/media/{rows['wamid.IMG'].id}"


BSUID = "IN.13491208655302741918"
NEW_BSUID = "IN.88812208655302741000"


def by_bsuid(wamid: str, body: str, *, bsuid: str = BSUID, wa: str | None = None, username: str | None = "sheena.n",
             name: str = "Sheena") -> dict:
    """A message as Meta sends it since April 2026: the BSUID always, the
    number only when it may be shared."""
    message = {"from_user_id": bsuid, "id": wamid, "timestamp": str(int(datetime.now(timezone.utc).timestamp())), "type": "text",
               "text": {"body": body}}
    contact = {"profile": {"name": name, **({"username": username} if username else {})}, "user_id": bsuid}
    if wa:
        message["from"], contact["wa_id"] = wa, wa
    value = {"messaging_product": "whatsapp", "metadata": {"display_phone_number": "919847012345", "phone_number_id": PHONE},
             "contacts": [contact], "messages": [message]}
    return {"object": "whatsapp_business_account", "entry": [{"id": WABA, "changes": [{"field": "messages", "value": value}]}]}


async def test_someone_with_a_username_is_kept_and_answered_by_their_bsuid(client, session, world, meta):
    account = await connected(client, session, world)
    r = await hook(client, account.webhook_token, by_bsuid("wamid.U1", "Is it free?"))
    assert r.status_code == 200 and r.json()["messages"] == 1
    conv = await session.scalar(select(ClubWhatsAppConversations).execution_options(populate_existing=True))
    assert conv.wa_id is None and conv.bsuid == BSUID and conv.username == "sheena.n" and conv.unread_count == 1
    item = (await client.get(f"{API}/clubs/whatsapp/conversations", headers=world.h)).json()["items"][0]
    assert item["wa_id"] is None and item["phone"] is None and item["bsuid"] == BSUID and item["username"] == "sheena.n"
    assert item["contact"]["bsuid"] == BSUID and item["contact"]["wa_id"] is None and item["contact"]["username"] == "sheena.n"
    assert (await client.get(f"{API}/clubs/whatsapp/conversations", params={"q": "sheena.n"}, headers=world.h)).json()["total"] == 1
    r = await client.post(f"{API}/clubs/whatsapp/conversations/{conv.id}/messages", json={"type": "text", "text": "Yes."}, headers=world.h)
    assert r.status_code == 200 and r.json()["status"] == "sent"
    # Meta's `recipient`, and no `to`: there is no number to send to.
    assert meta.club_sent[-1]["recipient"] == BSUID and "to" not in meta.club_sent[-1]
    # When Meta later shares their number, the same conversation takes it (and is linked by it).
    await hook(client, account.webhook_token, by_bsuid("wamid.U2", "It's Ananya, actually", wa=GUEST))
    convs = (await session.scalars(select(ClubWhatsAppConversations).execution_options(populate_existing=True))).all()
    assert len(convs) == 1 and convs[0].wa_id == GUEST and convs[0].bsuid == BSUID and convs[0].unread_count == 1
    assert convs[0].registration_id == world.reg.id
    r = await client.post(f"{API}/clubs/whatsapp/conversations/{conv.id}/messages", json={"type": "text", "text": "Hi!"}, headers=world.h)
    assert meta.club_sent[-1]["to"] == GUEST and "recipient" not in meta.club_sent[-1]


async def test_a_changed_number_or_bsuid_carries_the_conversation_on(client, session, world, meta):
    account = await connected(client, session, world)
    await hook(client, account.webhook_token, by_bsuid("wamid.C1", "hello", wa=GUEST))
    conv = await session.scalar(select(ClubWhatsAppConversations).execution_options(populate_existing=True))
    ts = str(int(datetime.now(timezone.utc).timestamp()))
    changed = {"from": GUEST, "id": "wamid.SYS1", "timestamp": ts, "type": "system", "system": {
        "body": "User A changed from +91 98765 43210 to +91 90000 00088", "wa_id": "919000000088", "user_id": NEW_BSUID,
        "previous_user_id": BSUID, "type": "user_changed_number"}}
    await hook(client, account.webhook_token, inbound(changed))
    conv = await session.get(ClubWhatsAppConversations, conv.id, populate_existing=True)
    assert conv.wa_id == "919000000088" and conv.bsuid == NEW_BSUID and conv.unread_count == 1
    # Their next message, from the new number, carries on the same thread.
    await hook(client, account.webhook_token, by_bsuid("wamid.C2", "new phone", wa="919000000088", bsuid=NEW_BSUID))
    # And a BSUID-only change.
    only = {"id": "wamid.SYS2", "timestamp": ts, "type": "system", "system": {
        "body": f"User changed from {NEW_BSUID} to IN.777", "user_id": "IN.7770001", "previous_user_id": NEW_BSUID,
        "type": "user_changed_user_id"}}
    value = {"messaging_product": "whatsapp", "metadata": {"phone_number_id": PHONE}, "messages": [only]}
    await hook(client, account.webhook_token, {"object": "whatsapp_business_account",
                                               "entry": [{"id": WABA, "changes": [{"field": "messages", "value": value}]}]})
    convs = (await session.scalars(select(ClubWhatsAppConversations).execution_options(populate_existing=True))).all()
    assert len(convs) == 1 and convs[0].bsuid == "IN.7770001" and convs[0].unread_count == 2
    kinds = [m.type for m in (await session.scalars(select(ClubWhatsAppMessages).order_by(ClubWhatsAppMessages.id))).all()]
    assert kinds == ["text", "system", "text", "system"]


async def test_a_message_on_another_number_of_the_same_app_is_not_this_clubs(client, session, world, meta):
    account = await connected(client, session, world)
    await hook(client, account.webhook_token, inbound(text_msg("wamid.OTHER", "hi"), phone_id="199999999999999"))
    assert await session.scalar(select(func.count()).select_from(ClubWhatsAppMessages)) == 0


async def test_attachments_come_through_the_authenticated_proxy_only(client, session, world, meta):
    account = await connected(client, session, world)
    await hook(client, account.webhook_token, inbound(text_msg("wamid.IMG", "", kind="image", part={"id": "media-77", "mime_type": "image/jpeg"})))
    msg = await session.scalar(select(ClubWhatsAppMessages))
    url = f"{API}/clubs/whatsapp/media/{msg.id}"
    r = await client.get(url, headers=world.h)
    assert r.status_code == 200 and r.content == meta.media_bytes and r.headers["content-type"] == "image/jpeg"
    assert r.headers["cache-control"] == "private, no-store"
    download = [c for c in meta.calls if c.url.host == "lookaside.fbsbx.com"][0]
    assert download.headers["authorization"] == f"Bearer {TOKEN}"
    assert r.headers["content-security-policy"] == "default-src 'none'; sandbox" and r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["content-disposition"] == "inline"
    assert (await client.get(url)).status_code == 401
    assert (await client.get(url, headers=world.other_h)).status_code == 404
    meta.fail["GET media-77"] = (404, {"error": {"code": 100, "message": "media not found"}})
    gone = await client.get(url, headers=world.h)
    assert gone.status_code == 410 and gone.json()["error_code"] == "MEDIA_GONE" and "7 days" in gone.json()["message"]
    # Expired media is not the token: the number stays as it was.
    assert (await account_of(session, world.club.id)).status == "active"


async def _attachment(client, session, w, account, *, mime="image/jpeg", filename=None) -> str:
    kind = "image" if mime.startswith("image/") else "document"
    part = {"id": "media-90", "mime_type": mime, **({"filename": filename} if filename else {})}
    await hook(client, account.webhook_token, inbound(text_msg(f"wamid.M{uuid.uuid4().hex[:6]}", "", kind=kind, part=part)))
    msg = await session.scalar(select(ClubWhatsAppMessages).order_by(ClubWhatsAppMessages.id.desc()))
    return f"{API}/clubs/whatsapp/media/{msg.id}"


@pytest.mark.parametrize("mime, filename, served, disposition", [
    ("image/jpeg", None, "image/jpeg", "inline"),
    ("image/webp", "sticker.webp", "image/webp", "inline"),
    ("audio/ogg; codecs=opus", None, "audio/ogg", "inline"),
    ("video/mp4", "clip.mp4", "video/mp4", "inline"),
    ("image/svg+xml", "logo.svg", "application/octet-stream", "attachment"),
    ("text/html", "page.html", "application/octet-stream", "attachment"),
    ("application/xhtml+xml", None, "application/octet-stream", "attachment"),
    ("application/pdf", "id.pdf", "application/pdf", "attachment"),
    ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "cv.docx",
     "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "attachment"),
])
async def test_only_pictures_sound_and_video_are_shown_in_place(client, session, world, meta, mime, filename, served, disposition):
    """Whatever Meta (or the sender) calls an attachment, nothing it holds
    runs on the dashboard's origin: SVG and HTML are downloads of bytes."""
    account = await connected(client, session, world)
    meta.media_info = {"mime_type": mime}
    meta.media_bytes = b"<svg onload=alert(1)>" if "svg" in mime or "html" in mime else b"bytes"
    r = await client.get(await _attachment(client, session, world, account, mime=mime, filename=filename), headers=world.h)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == served
    assert r.headers["content-disposition"].split(";")[0] == disposition
    assert r.headers["content-security-policy"] == "default-src 'none'; sandbox"
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["cache-control"] == "private, no-store"
    if filename:
        assert f"filename*=UTF-8''{filename}" in r.headers["content-disposition"]


async def test_a_malayalam_filename_is_saved_under_its_own_name(client, session, world, meta):
    account = await connected(client, session, world)
    meta.media_info = {"mime_type": "application/pdf", "file_size": "not a number"}
    name = "ടിക്കറ്റ് 2026.pdf"
    r = await client.get(await _attachment(client, session, world, account, mime="application/pdf", filename=name), headers=world.h)
    assert r.status_code == 200, r.text
    header = r.headers["content-disposition"]
    assert header.startswith('attachment; filename="')
    ascii_name = header.split('filename="', 1)[1].split('"', 1)[0]
    assert ascii_name.isascii() and ascii_name.endswith(".pdf")
    assert "filename*=UTF-8''" + "%E0%B4%9F" in header and header.endswith("%202026.pdf")
    assert unquote(header.split("filename*=UTF-8''", 1)[1]) == name


async def test_the_token_goes_to_metas_media_host_and_nowhere_else(client, session, world, meta):
    account = await connected(client, session, world)
    url = await _attachment(client, session, world, account)
    for elsewhere in ("https://attacker.example/steal", "http://lookaside.fbsbx.com/x", "https://lookaside.fbsbx.com.attacker.example/x"):
        meta.media_info = {"url": elsewhere}
        r = await client.get(url, headers=world.h)
        assert r.status_code == 502 and r.json()["error_code"] == "MEDIA_UNAVAILABLE"
    assert [c for c in meta.calls if c.url.host != "graph.facebook.com"] == []


async def test_an_address_that_expired_is_asked_for_once_more(client, session, world, meta):
    account = await connected(client, session, world)
    url = await _attachment(client, session, world, account)
    meta.download_answers = [(404, {"error": {"message": "URL expired"}})]
    r = await client.get(url, headers=world.h)
    assert r.status_code == 200 and r.content == meta.media_bytes
    assert len(meta.club_calls("GET", "/media-90")) == 2
    # Gone twice is gone.
    meta.download_answers = [(404, b"gone"), (404, b"gone")]
    r = await client.get(url, headers=world.h)
    assert r.status_code == 410 and r.json()["error_code"] == "MEDIA_GONE"


async def test_a_revoked_token_is_not_called_an_expired_attachment(client, session, world, meta):
    account = await connected(client, session, world)
    url = await _attachment(client, session, world, account)
    meta.fail["GET media-90"] = (401, {"error": {"code": 190, "message": "Error validating access token: The session has been invalidated"}})
    r = await client.get(url, headers=world.h)
    assert r.status_code == 409 and r.json()["error_code"] == "WHATSAPP_TOKEN_INVALID"
    assert (await account_of(session, world.club.id)).status == "failed"


async def test_an_attachment_from_a_replaced_number_says_so(client, session, world, meta):
    account = await connected(client, session, world)
    url = await _attachment(client, session, world, account)
    meta.waba_numbers = [PHONE, "109876543210999"]
    assert (await connect(client, world, phone_number_id="109876543210999")).status_code == 200
    meta.fail["GET media-90"] = (400, {"error": {"code": 100, "message": "Unsupported get request."}})
    r = await client.get(url, headers=world.h)
    assert r.status_code == 410 and r.json()["error_code"] == "MEDIA_OTHER_NUMBER"


async def test_no_message_content_reaches_the_log(client, session, world, meta, monkeypatch, caplog):
    token = (await connected(client, session, world)).webhook_token

    async def broken(*args, **kwargs):
        raise ValueError("Ananya's secret plan: meet at 9 at the gate")

    monkeypatch.setattr(inbox, "record_inbound", broken)
    with caplog.at_level(logging.DEBUG):
        r = await hook(client, token, inbound(text_msg("wamid.PRIV", "my private words 4417")))
    # Not kept, so Meta is asked to deliver it again.
    assert r.status_code == 503 and r.json() == {"ok": False, "messages": 0, "statuses": 0, "failed": 1}
    assert "secret plan" not in caplog.text and "private words 4417" not in caplog.text and GUEST not in caplog.text
    ours = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("app."))
    assert "ValueError" in ours and token not in ours
    assert redact.path(f"/api/v1/hooks/whatsapp/club/{token}") == f"/api/v1/hooks/whatsapp/club/{token[:6]}…"
    assert redact.is_sensitive("/api/v1/clubs/whatsapp/conversations/1/messages") and redact.is_sensitive(f"/api/v1/hooks/whatsapp/club/{token}")
    assert redact.is_sensitive("/api/v1/clubs/whatsapp/account")


def test_an_error_report_from_the_inbox_never_carries_the_message():
    from app.core.observability import scrub_event

    event = {
        "request": {"url": "https://api.myotherapp.in/api/v1/clubs/whatsapp/conversations/5/messages", "data": {"text": "meet me at 9"}},
        "exception": {"values": [{"type": "IntegrityError", "value": "duplicate key (body)=(meet me at 9)"}]},
    }
    out = scrub_event(event)
    assert "meet me at 9" not in json.dumps(out) and out["exception"]["values"][0]["type"] == "IntegrityError"
    hook_event = scrub_event({"request": {"url": "https://api.myotherapp.in/api/v1/hooks/whatsapp/club/AbCdEfGhIjKlMnOpQrSt"},
                              "exception": {"values": [{"type": "KeyError", "value": "'meet me at 9'"}]}})
    assert "meet me at 9" not in json.dumps(hook_event) and "AbCdEfGhIjKlMnOpQrSt" not in json.dumps(hook_event)
    other = scrub_event({"request": {"url": "https://api.myotherapp.in/api/v1/events/1"}, "exception": {"values": [{"type": "X", "value": "kept"}]}})
    assert other["exception"]["values"][0]["value"] == "kept"


# ─── Replies: the 24-hour window ─────────────────────────────────────────


async def test_free_text_inside_the_window_and_it_counts_as_an_answer(client, session, world, meta):
    account = await connected(client, session, world)
    conv = await conversation_with(client, session, world, account, at=datetime.now(timezone.utc) - timedelta(minutes=2))
    r = await client.post(f"{API}/clubs/whatsapp/conversations/{conv.id}/messages", json={"type": "text", "text": "Yes, behind the hall."},
                          headers=world.h)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "sent" and out["wamid"] == "wamid.CLUB1" and out["direction"] == "out" and out["via"] == "inbox"
    assert meta.club_sent[-1] == {"messaging_product": "whatsapp", "recipient_type": "individual", "to": GUEST, "type": "text",
                                  "text": {"preview_url": False, "body": "Yes, behind the hall."}}
    conv = await session.get(ClubWhatsAppConversations, conv.id, populate_existing=True)
    assert conv.awaiting_since is None and conv.unread_count == 0
    reply = await session.scalar(select(ClubWhatsAppMessages).where(ClubWhatsAppMessages.direction == "out"))
    assert 100 <= reply.response_seconds <= 200 and reply.sent_by_name
    stats = (await client.get(f"{API}/clubs/whatsapp/overview", headers=world.h)).json()["stats"]
    assert stats["replies"] == 1 and stats["unanswered"] == 0 and 100 <= stats["median_response_seconds"] <= 200
    assert stats["received"] == 1 and stats["conversations"] == 1


async def test_after_24_hours_free_text_is_refused_and_meta_is_not_asked(client, session, world, meta):
    account = await connected(client, session, world)
    conv = await conversation_with(client, session, world, account, at=datetime.now(timezone.utc) - timedelta(hours=25))
    assert not inbox.window_open(conv)
    sent_before = len(meta.club_sent)
    r = await client.post(f"{API}/clubs/whatsapp/conversations/{conv.id}/messages", json={"type": "text", "text": "Hello again"}, headers=world.h)
    assert r.status_code == 409 and r.json()["error_code"] == "WINDOW_CLOSED"
    assert len(meta.club_sent) == sent_before
    listed = (await client.get(f"{API}/clubs/whatsapp/conversations", headers=world.h)).json()["items"][0]
    assert listed["window_open"] is False and listed["window_expires_at"]


async def test_outside_the_window_an_approved_template_goes_with_its_values(client, session, world, meta):
    account = await connected(client, session, world)
    conv = await conversation_with(client, session, world, account, at=datetime.now(timezone.utc) - timedelta(days=3))
    url = f"{API}/clubs/whatsapp/conversations/{conv.id}/messages"
    r = await client.post(url, json={"type": "template", "template": "event_update", "language": "en",
                                     "params": {"body": ["Ananya", "Build Night"]}}, headers=world.h)
    assert r.status_code == 200, r.text
    assert r.json()["type"] == "template" and r.json()["body"] == "Hi Ananya, there's news about Build Night for you."
    assert meta.club_sent[-1]["template"] == {"name": "event_update", "language": {"code": "en"}, "components": [
        {"type": "body", "parameters": [{"type": "text", "text": "Ananya"}, {"type": "text", "text": "Build Night"}]}]}
    # The wrong number of values, a rejected template, one Meta never approved.
    bad = await client.post(url, json={"type": "template", "template": "event_update", "params": {"body": ["Ananya"]}}, headers=world.h)
    assert bad.status_code == 422 and bad.json()["errors"]["field"] == "body"
    for name in ("old_promo", "never_made"):
        r = await client.post(url, json={"type": "template", "template": name, "params": {"body": ["x"]}}, headers=world.h)
        assert r.status_code == 409 and r.json()["error_code"] == "TEMPLATE_NOT_APPROVED"


async def test_a_refused_send_is_kept_failed_with_metas_words(client, session, world, meta):
    account = await connected(client, session, world)
    conv = await conversation_with(client, session, world, account)
    meta.club_answer = lambda body: httpx.Response(400, json={"error": {"code": 131026, "message": "Message undeliverable"}})
    r = await client.post(f"{API}/clubs/whatsapp/conversations/{conv.id}/messages", json={"type": "text", "text": "Hi"}, headers=world.h)
    assert r.status_code == 200 and r.json()["status"] == "failed"
    assert r.json()["error"]["code"] == "131026" and "cannot receive" in r.json()["error"]["title"]
    conv = await session.get(ClubWhatsAppConversations, conv.id, populate_existing=True)
    assert conv.awaiting_since is not None  # not answered
    # An account-level refusal marks the number failed, and the club is told.
    meta.club_answer = lambda body: httpx.Response(401, json={"error": {"code": 190, "message": "Session has expired"}})
    r = await client.post(f"{API}/clubs/whatsapp/conversations/{conv.id}/messages", json={"type": "text", "text": "Hi"}, headers=world.h)
    assert r.json()["status"] == "failed" and (await account_of(session, world.club.id)).status == "failed"
    again = await client.post(f"{API}/clubs/whatsapp/conversations/{conv.id}/messages", json={"type": "text", "text": "Hi"}, headers=world.h)
    assert again.status_code == 409 and again.json()["error_code"] == "WHATSAPP_FAILED"


async def test_statuses_move_forward_only_and_a_failure_says_why(client, session, world, meta):
    account = await connected(client, session, world)
    conv = await conversation_with(client, session, world, account)
    await client.post(f"{API}/clubs/whatsapp/conversations/{conv.id}/messages", json={"type": "text", "text": "See you"}, headers=world.h)
    ts = str(int(datetime.now(timezone.utc).timestamp()))

    async def status(s, errors=None):
        st = {"id": "wamid.CLUB1", "status": s, "timestamp": ts, "recipient_id": GUEST}
        if errors:
            st["errors"] = errors
        await hook(client, account.webhook_token, inbound(statuses=[st]))
        return (await session.scalar(select(ClubWhatsAppMessages).where(ClubWhatsAppMessages.wamid == "wamid.CLUB1")
                                     .execution_options(populate_existing=True)))

    assert (await status("delivered")).status == "delivered"
    assert (await status("sent")).status == "delivered"  # late
    # Failed never undoes delivered: it reached their phone.
    assert (await status("failed", [{"code": 131047, "title": "Re-engagement message"}])).status == "delivered"
    assert (await status("read")).status == "read"
    assert (await status("failed", [{"code": 131047, "title": "Re-engagement message"}])).status == "read"
    conv = await session.get(ClubWhatsAppConversations, conv.id, populate_existing=True)
    assert conv.last_message_status == "read"


async def test_several_messages_in_one_delivery_all_count(client, session, world, meta):
    """One UPDATE per message, adding to what is there: three in one payload
    are three unread, waiting since the first. A reaction, a system notice
    and a message WhatsApp can't show are kept and counted for nothing."""
    account = await connected(client, session, world)
    t0 = datetime.now(timezone.utc) - timedelta(minutes=10)
    batch = [text_msg(f"wamid.B{n}", f"line {n}", at=t0 + timedelta(minutes=n)) for n in range(3)]
    batch += [
        text_msg("wamid.R", "", kind="reaction", part={"message_id": "wamid.B0", "emoji": "👍"}, at=t0 + timedelta(minutes=4)),
        text_msg("wamid.U", "", kind="unsupported", part={}, at=t0 + timedelta(minutes=5)),
    ]
    r = await hook(client, account.webhook_token, inbound(*batch))
    assert r.json()["messages"] == 5
    conv = await session.scalar(select(ClubWhatsAppConversations).execution_options(populate_existing=True))
    assert conv.unread_count == 3 and abs((conv.awaiting_since - t0).total_seconds()) < 1
    assert conv.last_message_type == "unsupported" and abs((conv.last_inbound_at - (t0 + timedelta(minutes=5))).total_seconds()) < 1


async def test_a_reaction_opens_the_window_but_a_system_notice_does_not(client, session, world, meta):
    account = await connected(client, session, world)
    reaction = text_msg("wamid.REA", "", kind="reaction", part={"message_id": "wamid.X", "emoji": "❤️"})
    await hook(client, account.webhook_token, inbound(reaction))
    conv = await session.scalar(select(ClubWhatsAppConversations).execution_options(populate_existing=True))
    assert conv.unread_count == 0 and conv.awaiting_since is None and inbox.window_open(conv)
    system = text_msg("wamid.SYS", "", kind="system", part={"body": "User changed number", "type": "customer_changed_number"}, wa="919000000077")
    await hook(client, account.webhook_token, inbound(system, wa="919000000077"))
    other = await session.scalar(select(ClubWhatsAppConversations).where(ClubWhatsAppConversations.wa_id == "919000000077")
                                 .execution_options(populate_existing=True))
    assert other.unread_count == 0 and other.last_inbound_at is None and not inbox.window_open(other)


async def test_a_reply_leaves_a_message_that_came_meanwhile_waiting(client, session, world, meta, monkeypatch):
    account = await connected(client, session, world)
    conv = await conversation_with(client, session, world, account, at=datetime.now(timezone.utc) - timedelta(minutes=5))
    real_send = graph.send
    meanwhile = datetime.now(timezone.utc) - timedelta(seconds=30)

    async def send(keys, body):
        # They write again while the reply is on its way to Meta.
        await inbox.record_inbound(session, account, PHONE, text_msg("wamid.MEANWHILE", "also, food?", at=meanwhile), {"name": "Ananya"})
        return await real_send(keys, body)

    monkeypatch.setattr(graph, "send", send)
    r = await client.post(f"{API}/clubs/whatsapp/conversations/{conv.id}/messages", json={"type": "text", "text": "Yes, free."}, headers=world.h)
    assert r.status_code == 200 and r.json()["status"] == "sent"
    conv = await session.get(ClubWhatsAppConversations, conv.id, populate_existing=True)
    assert conv.unread_count == 1 and abs((conv.awaiting_since - meanwhile).total_seconds()) < 1
    reply = await session.scalar(select(ClubWhatsAppMessages).where(ClubWhatsAppMessages.direction == "out"))
    assert 250 <= reply.response_seconds <= 400


async def test_a_thread_is_in_the_order_things_happened(client, session, world, meta):
    """Meta may deliver a later message first: the thread goes by when they
    were written, and catching up by id still finds the late one."""
    account = await connected(client, session, world)
    t0 = datetime.now(timezone.utc) - timedelta(minutes=30)
    await hook(client, account.webhook_token, inbound(text_msg("wamid.SECOND", "second", at=t0 + timedelta(minutes=2))))
    conv = await session.scalar(select(ClubWhatsAppConversations).execution_options(populate_existing=True))
    url = f"{API}/clubs/whatsapp/conversations/{conv.id}/messages"
    seen = (await client.get(url, headers=world.h)).json()["items"]
    await hook(client, account.webhook_token, inbound(text_msg("wamid.FIRST", "first", at=t0)))
    await hook(client, account.webhook_token, inbound(text_msg("wamid.THIRD", "third", at=t0 + timedelta(minutes=3))))
    thread = (await client.get(url, headers=world.h)).json()
    assert [m["wamid"] for m in thread["items"]] == ["wamid.FIRST", "wamid.SECOND", "wamid.THIRD"] and thread["has_more"] is False
    caught_up = (await client.get(url, params={"after": seen[-1]["id"]}, headers=world.h)).json()["items"]
    assert [m["wamid"] for m in caught_up] == ["wamid.FIRST", "wamid.THIRD"]
    page = (await client.get(url, params={"limit": 2}, headers=world.h)).json()
    assert [m["wamid"] for m in page["items"]] == ["wamid.SECOND", "wamid.THIRD"] and page["has_more"] is True
    older = (await client.get(url, params={"limit": 2, "before": page["items"][0]["id"]}, headers=world.h)).json()
    assert [m["wamid"] for m in older["items"]] == ["wamid.FIRST"] and older["has_more"] is False


async def test_reading_a_conversation_clears_it_and_tells_meta(client, session, world, meta):
    account = await connected(client, session, world)
    conv = await conversation_with(client, session, world, account)
    r = await client.post(f"{API}/clubs/whatsapp/conversations/{conv.id}/read", headers=world.h)
    assert r.status_code == 200 and r.json()["unread_count"] == 0
    receipt = [json.loads(c.content) for c in meta.club_calls("POST", f"/{PHONE}/messages") if b'"status"' in c.content]
    assert receipt and receipt[-1]["status"] == "read"
    closed = await client.patch(f"{API}/clubs/whatsapp/conversations/{conv.id}", json={"status": "closed"}, headers=world.h)
    assert closed.json()["status"] == "closed"
    assert (await client.get(f"{API}/clubs/whatsapp/conversations?filter=open", headers=world.h)).json()["total"] == 0
    assert (await client.get(f"{API}/clubs/whatsapp/conversations?filter=closed", headers=world.h)).json()["total"] == 1
    assert (await client.get(f"{API}/clubs/whatsapp/conversations?q=Ananya", headers=world.h)).json()["total"] == 1
    assert (await client.get(f"{API}/clubs/whatsapp/conversations?q=98765", headers=world.h)).json()["total"] == 1
    assert (await client.get(f"{API}/clubs/whatsapp/conversations?q=nobody", headers=world.h)).json()["total"] == 0


async def test_starting_a_conversation_needs_the_guests_opt_in(client, session, world, meta, make_user, make_registration):
    await connected(client, session, world)
    r = await client.post(f"{API}/clubs/whatsapp/conversations", json={"registration_id": str(world.reg.id)}, headers=world.h)
    assert r.status_code == 200 and r.json()["wa_id"] == GUEST and r.json()["window_open"] is False
    quiet = await make_user(email="quiet@example.com")
    no = await make_registration(event=world.event, user=quiet, full_name="Q", email=quiet.email, phone="9000000001", whatsapp_opt_in=False)
    await session.commit()
    r = await client.post(f"{API}/clubs/whatsapp/conversations", json={"registration_id": str(no.id)}, headers=world.h)
    assert r.status_code == 409 and r.json()["error_code"] == "NO_OPT_IN"
    r = await client.post(f"{API}/clubs/whatsapp/conversations", json={"registration_id": str(world.other_reg.id)}, headers=world.h)
    assert r.status_code == 404


# ─── Templates ───────────────────────────────────────────────────────────


async def test_templates_are_listed_from_meta_with_status_and_reason(client, session, world, meta):
    await connected(client, session, world)
    r = await client.get(f"{API}/clubs/whatsapp/templates", headers=world.h)
    assert r.status_code == 200
    out = r.json()
    by = {t["name"]: t for t in out["items"]}
    assert by["old_promo"]["status"] == "REJECTED" and by["old_promo"]["rejected_reason"] == "INVALID_FORMAT"
    assert by["event_update"]["body_variables"] == 2 and by["ticket_confirmed"]["header"]["format"] == "DOCUMENT"
    assert by["event_reminder"]["buttons"][0]["variables"] == 1
    assert out["stale"] is False and out["standard_missing"] == ["certificate_ready"]
    meta.fail[f"GET {WABA}/message_templates"] = "network"
    stale = (await client.get(f"{API}/clubs/whatsapp/templates", headers=world.h)).json()
    assert stale["stale"] is True and {t["name"] for t in stale["items"]} == set(by)


async def test_a_template_is_checked_then_submitted(client, session, world, meta):
    await connected(client, session, world)
    good = {
        "name": "venue_change", "language": "en", "category": "UTILITY",
        "header": {"text": "{{1}} has moved", "examples": ["Build Night"]},
        "body": {"text": "Hi {{1}}, {{2}} is now at {{3}} -- same time.", "examples": ["Ananya", "Build Night", "Hall B"]},
        "footer": "Yukthi, CET",
        "buttons": [{"type": "URL", "text": "See the event", "url": "https://myotherapp.in/events/{{1}}",
                     "example": "https://myotherapp.in/events/build-night"},
                    {"type": "QUICK_REPLY", "text": "Thanks"}],
    }
    r = await client.post(f"{API}/clubs/whatsapp/templates", json=good, headers=world.h)
    assert r.status_code == 200, r.text
    sent = meta.created[-1]
    assert sent["name"] == "venue_change" and sent["category"] == "UTILITY"
    assert sent["components"] == [
        {"type": "HEADER", "format": "TEXT", "text": "{{1}} has moved", "example": {"header_text": ["Build Night"]}},
        {"type": "BODY", "text": "Hi {{1}}, {{2}} is now at {{3}} -- same time.", "example": {"body_text": [["Ananya", "Build Night", "Hall B"]]}},
        {"type": "FOOTER", "text": "Yukthi, CET"},
        {"type": "BUTTONS", "buttons": [
            {"type": "URL", "text": "See the event", "url": "https://myotherapp.in/events/{{1}}", "example": ["https://myotherapp.in/events/build-night"]},
            {"type": "QUICK_REPLY", "text": "Thanks"}]},
    ]
    assert await session.scalar(select(func.count()).select_from(AdminAuditLogs).where(AdminAuditLogs.action == "club_whatsapp.template_create")) == 1


@pytest.mark.parametrize("change, field", [
    ({"name": "Venue Change"}, "name"),
    ({"language": "english"}, "language"),
    ({"category": "AUTHENTICATION"}, "category"),
    ({"body": {"text": "{{1}}, it moved.", "examples": ["Ananya"]}}, "body"),
    ({"body": {"text": "It moved to {{1}}", "examples": ["Hall B"]}}, "body"),
    ({"body": {"text": "Hi {{1}}, see {{3}} now.", "examples": ["a", "b"]}}, "body"),
    ({"body": {"text": "Hi {{name}}, it moved.", "examples": ["a"]}}, "body"),
    ({"body": {"text": "Hi {{1}}, it moved to {{2}} today.", "examples": ["Ananya"]}}, "body_examples"),
    ({"footer": "Reply {{1}}"}, "footer"),
    ({"buttons": [{"type": "URL", "text": "Go", "url": "http://example.com"}]}, "buttons.0"),
    ({"buttons": [{"type": "URL", "text": "Go", "url": "https://x.example/{{1}}/more"}]}, "buttons.0"),
    ({"buttons": [{"type": "CALL", "text": "Go"}]}, "buttons.0"),
])
async def test_a_template_meta_would_refuse_is_refused_here_naming_the_field(client, session, world, meta, change, field):
    await connected(client, session, world)
    base = {"name": "venue_change", "language": "en", "category": "UTILITY",
            "body": {"text": "Hi {{1}}, it moved.", "examples": ["Ananya"]}}
    r = await client.post(f"{API}/clubs/whatsapp/templates", json={**base, **change}, headers=world.h)
    assert r.status_code == 422 and r.json()["error_code"] == "INVALID_TEMPLATE"
    assert r.json()["errors"]["field"] == field
    assert meta.created == []


async def test_a_template_is_deleted_in_one_language_or_all(client, session, world, meta):
    await connected(client, session, world)
    r = await client.delete(f"{API}/clubs/whatsapp/templates/event_update?language=en", headers=world.h)
    assert r.status_code == 200
    assert meta.deleted[-1] == {"name": "event_update", "hsm_id": "tpl-event_update-en", "appsecret_proof": meta.deleted[-1]["appsecret_proof"]}
    await client.delete(f"{API}/clubs/whatsapp/templates/old_promo", headers=world.h)
    assert "hsm_id" not in meta.deleted[-1] and meta.deleted[-1]["name"] == "old_promo"


async def test_myotherapps_own_templates_are_submitted_with_a_sample_document(client, session, world, meta):
    await connected(client, session, world)
    no_app = (await client.post(f"{API}/clubs/whatsapp/templates/standard", headers=world.h)).json()["results"]
    assert no_app["ticket_confirmed"] == "exists" and "app ID" in no_app["certificate_ready"]
    await connect(client, world, app_id=APP_ID)
    out = (await client.post(f"{API}/clubs/whatsapp/templates/standard", headers=world.h)).json()["results"]
    assert out == {"ticket_confirmed": "exists", "event_reminder": "exists", "certificate_ready": "submitted"}
    created = meta.created[-1]
    assert created["name"] == "certificate_ready" and created["category"] == "UTILITY"
    assert created["components"][0] == {"type": "HEADER", "format": "DOCUMENT", "example": {"header_handle": ["4::aGFuZGxl"]}}
    upload = [c for c in meta.calls if c.url.path.endswith("upload:MTphdHRhY2htZW50")][0]
    assert upload.headers["authorization"] == f"OAuth {TOKEN}" and upload.content.startswith(b"%PDF")


async def test_metas_template_verdicts_arrive_by_webhook(client, session, world, meta):
    account = await connected(client, session, world)
    payload = {"object": "whatsapp_business_account", "entry": [{"id": WABA, "changes": [{"field": "message_template_status_update", "value": {
        "event": "REJECTED", "message_template_id": 123, "message_template_name": "event_update", "message_template_language": "en",
        "reason": "INCORRECT_CATEGORY"}}]}]}
    await hook(client, account.webhook_token, payload)
    row = await session.scalar(select(ClubWhatsAppTemplates).where(ClubWhatsAppTemplates.name == "event_update").execution_options(populate_existing=True))
    assert row.status == "REJECTED" and row.rejected_reason == "INCORRECT_CATEGORY"


# ─── Tickets, certificates and reminders from the club's number ──────────


async def _reminder(session, world) -> Deliveries:
    world.reg.created_at = NOW - timedelta(days=10)
    await session.commit()
    out = await deliveries.queue(session, world.event, DeliveryKind.reminder, registration_ids=[world.reg.id])
    assert "whatsapp" in out["channels"]
    row = await session.scalar(select(Deliveries).where(Deliveries.registration_id == world.reg.id, Deliveries.channel == DeliveryChannel.whatsapp))
    return await deliveries.deliver(session, row.id)


async def test_a_reminder_goes_from_the_clubs_own_number_and_shows_in_its_inbox(client, session, world, meta):
    await connected(client, session, world)
    # The platform has no number at all: the club's own carries it.
    assert not whatsapp.configured()
    d = await _reminder(session, world)
    assert d.status == DeliveryStatus.sent and d.provider_id == "wamid.CLUB1"
    sent = meta.club_sent[-1]
    assert sent["to"] == GUEST and sent["template"]["name"] == "event_reminder" and sent["template"]["language"] == {"code": "en"}
    assert sent["template"]["components"][0]["parameters"][0] == {"type": "text", "text": "Ananya"}
    assert meta.platform_sent == []
    msg = await session.scalar(select(ClubWhatsAppMessages).where(ClubWhatsAppMessages.via == "delivery"))
    assert msg.wamid == "wamid.CLUB1" and msg.delivery_id == d.id and msg.body.startswith("Hi Ananya, a reminder that Build Night starts")
    # Meta's status for it, on the club's webhook, reaches the delivery too.
    account = await account_of(session, world.club.id)
    await hook(client, account.webhook_token, inbound(statuses=[{"id": "wamid.CLUB1", "status": "delivered",
                                                                 "timestamp": str(int(NOW.timestamp())), "recipient_id": GUEST}]))
    d = await session.get(Deliveries, d.id, populate_existing=True)
    assert d.outcome is not None and d.outcome.value == "delivered"


async def test_a_ticket_goes_from_the_clubs_number_with_its_document(client, session, world, meta):
    from app.core.layout.model import TicketLayoutFields, dump
    from app.core.layout.starters import ticket_starter

    await connected(client, session, world)
    st = ticket_starter("classic")
    r = await client.put(f"{API}/events/{world.event.id}/designs/ticket/layout",
                         json=dump(TicketLayoutFields(canvas=st["canvas"], layers=st["layers"], page=st["page"])), headers=world.h)
    assert r.status_code == 200, r.text
    await deliveries.queue(session, world.event, DeliveryKind.ticket, registration_ids=[world.reg.id])
    row = await session.scalar(select(Deliveries).where(Deliveries.registration_id == world.reg.id, Deliveries.channel == DeliveryChannel.whatsapp))
    d = await deliveries.deliver(session, row.id)
    assert d.status == DeliveryStatus.sent and d.provider_id == "wamid.CLUB1"
    upload = meta.club_calls("POST", f"/{PHONE}/media")[0]
    assert upload.headers["authorization"] == f"Bearer {TOKEN}" and b"%PDF" in upload.content
    sent = meta.club_sent[-1]["template"]
    assert sent["name"] == "ticket_confirmed" and sent["components"][0]["parameters"][0]["document"]["id"] == "media-up-1"
    assert sent["components"][2] == {"type": "button", "sub_type": "url", "index": "0",
                                     "parameters": [{"type": "text", "text": world.reg.ticket_id}]}
    summary = await deliveries.summary(session, world.event, DeliveryKind.ticket)
    assert next(c for c in summary["channels"] if c["channel"] == "whatsapp")["sent"] == 1


async def test_other_clubs_still_send_from_the_platforms_number(client, session, world, meta, platform_number, make_user, make_registration):
    await connected(client, session, world)
    other = await make_registration(event=world.other_event, user=await make_user(email="t@example.com"), full_name="Tara", email="t@example.com",
                                    phone="9000000009", whatsapp_opt_in=True, created_at=NOW - timedelta(days=10))
    await session.commit()
    await deliveries.queue(session, world.other_event, DeliveryKind.reminder, registration_ids=[other.id])
    row = await session.scalar(select(Deliveries).where(Deliveries.registration_id == other.id, Deliveries.channel == DeliveryChannel.whatsapp))
    d = await deliveries.deliver(session, row.id)
    assert d.status == DeliveryStatus.sent and d.provider_id == "wamid.PLATFORM1" and meta.club_sent == []


async def test_an_account_problem_falls_back_to_the_platform_and_tells_the_club(client, session, world, meta, platform_number, mails):
    await connected(client, session, world)
    meta.club_answer = lambda body: httpx.Response(401, json={"error": {"code": 190, "message": "Error validating access token"}})
    d = await _reminder(session, world)
    assert d.status == DeliveryStatus.sent and d.provider_id == "wamid.PLATFORM1"
    assert meta.platform_sent[-1]["template"]["name"] == "event_reminder"
    account = await account_of(session, world.club.id)
    assert account.status == "failed" and account.last_error_code == "190"
    assert await session.scalar(select(func.count()).select_from(AdminNotifications).where(AdminNotifications.kind == "club_whatsapp_failed")) == 1
    await after_commit.drain()
    told = [m for m in mails if "stopped working on MyOtherApp" in m["subject"]]
    assert len(told) == 1 and told[0]["recipients"] == ["yukthi@example.com"] and "Session" not in told[0]["subject"]
    # And in the club's own bell, leading to its WhatsApp settings.
    bell = (await client.get(f"{API}/clubs/alerts", headers=world.h)).json()
    assert [(a["kind"], a["link"]) for a in bell["items"]] == [("whatsapp_failed", "/whatsapp/settings")]
    # Once failed, it is not tried again: the next goes from the platform straight away.
    assert await accounts.route_for(session, world.club.id, "event_reminder") is None


async def test_a_missing_template_falls_back_without_failing_the_number(client, session, world, meta, platform_number):
    await connected(client, session, world)
    meta.club_answer = lambda body: httpx.Response(404, json={"error": {"code": 132001, "message": "Template name does not exist"}})
    d = await _reminder(session, world)
    assert d.status == DeliveryStatus.sent and d.provider_id == "wamid.PLATFORM1"
    assert (await account_of(session, world.club.id)).status == "active"
    row = await session.scalar(select(ClubWhatsAppTemplates).where(ClubWhatsAppTemplates.name == "event_reminder").execution_options(populate_existing=True))
    assert row.status == "UNUSABLE"


async def test_a_note_that_cannot_be_written_never_makes_a_sent_ticket_failed(client, session, world, meta, platform_number, monkeypatch):
    """The inbox's line, and the failure's note, are written after Meta took
    (or refused) the send: a commit of either that fails must leave the
    send as it was, or "retry failed" would send the guest a second copy."""
    from app.api.club_whatsapp import inbox as club_inbox

    await connected(client, session, world)

    async def bad_row(s, *args, **kwargs):
        # A row that fails only when it is flushed, after the note's own code is done.
        s.add(ClubWhatsAppMessages(conversation_id=987654321, club_id=world.club.id, direction="out", type="template", status="sent"))

    async def failing_commit(s, *args, **kwargs):
        # The note's transaction cannot commit (the database went away).
        real = s.commit

        async def commit():
            s.commit = real
            raise ConnectionResetError("the database closed the connection")

        s.commit = commit

    async def again(d):
        row = await session.scalar(select(Deliveries).where(Deliveries.id == d.id))
        row.status, row.queued_at = DeliveryStatus.pending, datetime.now(timezone.utc)
        await session.commit()
        return await deliveries.deliver(session, d.id)

    monkeypatch.setattr(club_inbox, "record_delivery_send", bad_row)
    d = await _reminder(session, world)
    assert d.status == DeliveryStatus.sent and d.provider_id == "wamid.CLUB1" and d.error is None
    monkeypatch.setattr(club_inbox, "record_delivery_send", failing_commit)
    d = await again(d)
    assert d.status == DeliveryStatus.sent and d.provider_id == "wamid.CLUB2" and d.error is None
    # And the failure's own note, when the club's number refuses it.
    monkeypatch.setattr(accounts, "after_failure", bad_row)
    meta.club_answer = lambda body: httpx.Response(401, json={"error": {"code": 190, "message": "expired"}})
    d = await again(d)
    assert d.status == DeliveryStatus.sent and d.provider_id == "wamid.PLATFORM1"


@pytest.mark.parametrize("code", [131030, 131021])
async def test_a_refusal_only_the_clubs_number_meets_goes_from_the_platform_and_marks_nothing(
        client, session, world, meta, platform_number, code):
    await connected(client, session, world)
    meta.club_answer = lambda body: httpx.Response(400, json={"error": {"code": code, "message": "Recipient phone number not in allowed list"}})
    d = await _reminder(session, world)
    assert d.status == DeliveryStatus.sent and d.provider_id == "wamid.PLATFORM1"
    await session.refresh(world.reg)
    assert world.reg.wa_bounced_at is None and world.reg.wa_bounce_reason is None
    assert (await account_of(session, world.club.id)).status == "active"
    row = await session.scalar(select(ClubWhatsAppTemplates).where(ClubWhatsAppTemplates.name == "event_reminder").execution_options(populate_existing=True))
    assert row.status == "APPROVED"


async def test_the_platforms_number_is_held_back_by_the_plan_before_a_fallback(client, session, world, meta, platform_number, monkeypatch):
    """A reminder from the club's own number is never held back; the same
    reminder falling back to the platform's is a platform send, and the
    club's plan is asked first."""
    from app.api.message_quota import service as message_quota

    asked = []
    real_check = message_quota.check

    async def check(session, club_id, channel, kind, *, via="platform", **kwargs):
        asked.append(via)
        if via == "platform":
            return None, "Not sent: this month's WhatsApp messages are used up."
        return await real_check(session, club_id, channel, kind, via=via, **kwargs)

    monkeypatch.setattr(message_quota, "check", check)
    await connected(client, session, world)
    meta.club_answer = lambda body: httpx.Response(401, json={"error": {"code": 190, "message": "expired"}})
    d = await _reminder(session, world)
    assert asked == ["club", "platform"]
    assert d.status == DeliveryStatus.skipped and "used up" in d.error and meta.platform_sent == []


async def test_a_send_decides_each_clubs_route_once_and_forgets_it_on_a_refusal(client, session, world, meta, monkeypatch):
    await connected(client, session, world)
    looked = []
    real = accounts.routing_account

    async def routing_account(session, club_id):
        looked.append(club_id)
        return await real(session, club_id)

    monkeypatch.setattr(accounts, "routing_account", routing_account)
    cache: dict = {}
    first = await accounts.route_for(session, world.club.id, "event_reminder", cache=cache)
    assert first is not None and await accounts.route_for(session, world.club.id, "event_reminder", cache=cache) is first
    assert looked == [world.club.id]
    # A template refusal forgets that template; an account refusal forgets the club.
    first.forget(whatsapp.Refused("No approved template (132001)", "132001"))
    assert await accounts.route_for(session, world.club.id, "event_reminder", cache=cache) is None
    other = await accounts.route_for(session, world.club.id, "ticket_confirmed", cache=cache)
    assert other is not None
    other.forget(whatsapp.Refused("token expired (190)", "190"))
    assert await accounts.route_for(session, world.club.id, "certificate_ready", cache=cache) is None
    assert await accounts.route_for(session, world.club.id, "ticket_confirmed", cache=cache) is None


async def test_a_template_the_club_lacks_goes_from_the_platform_or_says_why_not(client, session, world, meta, platform_number, monkeypatch):
    meta.templates = [t for t in meta.templates if t["name"] != "event_reminder"]
    await connected(client, session, world)
    d = await _reminder(session, world)
    assert d.provider_id == "wamid.PLATFORM1" and meta.club_sent == []
    # With no platform number either, the guest is skipped, and the club told why.
    monkeypatch.setattr(settings, "WHATSAPP_ACCESS_TOKEN", "")
    row = await session.scalar(select(Deliveries).where(Deliveries.id == d.id))
    row.status = DeliveryStatus.pending
    await session.commit()
    d = await deliveries.deliver(session, d.id)
    assert d.status == DeliveryStatus.skipped and "no approved event_reminder template" in d.error


async def test_routing_off_keeps_the_club_on_the_platforms_number(client, session, world, meta, platform_number):
    await connected(client, session, world)
    r = await client.patch(f"{API}/clubs/whatsapp/account", json={"routing": False, "retention_months": 6}, headers=world.h)
    assert r.json()["routing"] is False and r.json()["retention_months"] == 6
    d = await _reminder(session, world)
    assert d.provider_id == "wamid.PLATFORM1" and meta.club_sent == []
    assert await session.scalar(select(func.count()).select_from(AdminAuditLogs).where(AdminAuditLogs.action == "club_whatsapp.settings")) == 1


async def test_what_the_clubs_own_number_sends_never_counts_against_its_quota(client, session, world, meta, platform_number, monkeypatch):
    """The club pays Meta for its own number: a reply from the inbox goes
    through the quota hook as via="club", a reminder from the club's number
    answers "Via": "club", and neither is counted (app/api/message_quota).
    The same reminder from the platform's number counts."""
    from app.api.message_quota import service as message_quota
    from app.api.message_quota.models import MessageUsage

    seen = []
    real_count = message_quota.count

    async def count(session, **kwargs):
        seen.append((kwargs["kind"], kwargs["via"]))
        return await real_count(session, **kwargs)

    monkeypatch.setattr(message_quota, "count", count)
    account = await connected(client, session, world)
    conv = await conversation_with(client, session, world, account)
    await client.post(f"{API}/clubs/whatsapp/conversations/{conv.id}/messages", json={"type": "text", "text": "Hi"}, headers=world.h)
    d = await _reminder(session, world)
    assert d.provider_id == "wamid.CLUB2"
    assert ("inbox_reply", "club") in seen and ("reminder", "club") in seen
    used = await session.scalar(select(func.coalesce(func.sum(MessageUsage.used), 0)).where(
        MessageUsage.club_id == world.club.id, MessageUsage.channel == "whatsapp"))
    assert used == 0
    # From the platform's number (routing off), the next one is the platform's, and counts.
    await client.patch(f"{API}/clubs/whatsapp/account", json={"routing": False}, headers=world.h)
    row = await session.scalar(select(Deliveries).where(Deliveries.id == d.id))
    row.status = DeliveryStatus.pending
    row.queued_at = datetime.now(timezone.utc)
    await session.commit()
    d = await deliveries.deliver(session, d.id)
    assert d.provider_id == "wamid.PLATFORM1" and ("reminder", "platform") in seen
    used = await session.scalar(select(func.coalesce(func.sum(MessageUsage.used), 0)).where(
        MessageUsage.club_id == world.club.id, MessageUsage.channel == "whatsapp"))
    assert used == 1
    assert quota.VIA == "club" and quota.CHANNEL == "whatsapp"


# ─── Quality ─────────────────────────────────────────────────────────────


async def test_the_sweep_reads_quality_records_changes_and_tells_of_a_drop(client, session, world, meta, mails):
    await connected(client, session, world)
    account = await account_of(session, world.club.id)
    later = datetime.now(timezone.utc) + timedelta(hours=7)
    meta.number = {**meta.number, "quality_rating": "RED", "messaging_limit_tier": "TIER_250"}
    assert await sweep.refresh_quality(session, later) == 1
    account = await account_of(session, world.club.id)
    assert account.quality_rating == "RED" and account.messaging_limit_tier == "TIER_250"
    log = (await session.scalars(select(ClubWhatsAppQualityLog).order_by(ClubWhatsAppQualityLog.id))).all()
    assert [(row.previous_rating, row.quality_rating, row.source) for row in log] == [(None, "GREEN", "connect"), ("GREEN", "RED", "sweep")]
    assert await session.scalar(select(func.count()).select_from(AdminNotifications).where(AdminNotifications.kind == "club_whatsapp_quality")) == 1
    await after_commit.drain()
    assert any("quality dropped" in m["subject"] for m in mails)
    # Not due again for a while; asked again, an improvement tells nobody.
    assert await sweep.refresh_quality(session, later + timedelta(hours=1)) == 0
    meta.number = {**meta.number, "quality_rating": "YELLOW"}
    r = await client.post(f"{API}/clubs/whatsapp/account/verify", headers=world.h)
    assert r.json()["quality_rating"] == "YELLOW"
    assert await session.scalar(select(func.count()).select_from(AdminNotifications).where(AdminNotifications.kind == "club_whatsapp_quality")) == 1
    history = (await client.get(f"{API}/clubs/whatsapp/overview", headers=world.h)).json()["quality_history"]
    assert [h["quality_rating"] for h in history] == ["YELLOW", "RED", "GREEN"]


async def test_a_failed_number_meta_answers_for_again_is_active_again(client, session, world, meta):
    await connected(client, session, world)
    meta.fail[f"GET {PHONE}"] = (401, {"error": {"code": 190, "message": "expired"}})
    r = await client.post(f"{API}/clubs/whatsapp/account/verify", headers=world.h)
    assert r.json()["status"] == "failed" and "access token" in r.json()["last_error"]
    del meta.fail[f"GET {PHONE}"]
    assert await sweep.refresh_quality(session, datetime.now(timezone.utc) + timedelta(hours=7)) == 1
    assert (await account_of(session, world.club.id)).status == "active"


async def test_metas_limit_change_by_webhook_is_recorded(client, session, world, meta):
    account = await connected(client, session, world)
    payload = {"object": "whatsapp_business_account", "entry": [{"id": WABA, "changes": [{"field": "phone_number_quality_update", "value": {
        "display_phone_number": "919847012345", "event": "UPGRADE", "current_limit": "TIER_10K"}}]}]}
    await hook(client, account.webhook_token, payload)
    account = await account_of(session, world.club.id)
    assert account.messaging_limit_tier == "TIER_10K" and account.quality_checked_at is None


# ─── Who may see what ────────────────────────────────────────────────────


async def test_messages_are_for_the_owner_and_members_with_the_permission(client, session, world, meta):
    account = await connected(client, session, world)
    await conversation_with(client, session, world, account)
    url = f"{API}/clubs/whatsapp/conversations"
    editor = await member(session, world, "editor")
    r = await client.get(url, headers=_h(editor))
    assert r.status_code == 403 and r.json()["error_code"] == "WHATSAPP_PERMISSION"
    # The owner gives it; a member cannot.
    grant = f"{API}/clubs/access/{editor['_member_id']}/whatsapp"
    assert (await client.put(grant, json={"allowed": True}, headers=_h(editor))).status_code == 403
    r = await client.put(grant, json={"allowed": True}, headers=world.h)
    assert r.status_code == 200 and r.json()["whatsapp"] is True
    assert (await client.get(url, headers=_h(editor))).status_code == 200
    me = (await client.get(f"{API}/clubs/access/me", headers=_h(editor))).json()
    assert me["can"]["whatsapp_inbox"] is True and me["can"]["whatsapp_account"] is False
    assert await session.scalar(select(func.count()).select_from(AdminAuditLogs).where(AdminAuditLogs.action == "club_whatsapp.permission")) == 1
    # Door staff with the permission answer too; the connection stays the owner's and admins'.
    door = await member(session, world, "door", whatsapp_permission=True)
    assert (await client.get(url, headers=_h(door))).status_code == 200
    assert (await client.put(f"{API}/clubs/whatsapp/account", json=KEYS, headers=_h(door))).json()["error_code"] == "ROLE_FORBIDDEN"
    assert (await client.delete(f"{API}/clubs/whatsapp/account", headers=_h(editor))).status_code == 403
    page = (await client.get(f"{API}/clubs/whatsapp/account", headers=_h(editor))).json()
    assert page["can_manage"] is False and page["verify_token"] is None and page["webhook_url"]
    admin = await member(session, world, "admin")
    assert (await client.get(url, headers=_h(admin))).status_code == 403
    assert (await client.post(f"{API}/clubs/whatsapp/account/verify", headers=_h(admin))).status_code == 200


async def test_an_admin_acting_as_the_club_sees_the_number_never_the_messages(client, session, world, meta, make_admin, monkeypatch):
    from tests.test_plan_enforcement import impersonating

    account = await connected(client, session, world)
    conv = await conversation_with(client, session, world, account)
    await hook(client, account.webhook_token, inbound(text_msg("wamid.PIC", "", kind="image", part={"id": "media-5", "mime_type": "image/jpeg"})))
    picture = await session.scalar(select(ClubWhatsAppMessages).where(ClubWhatsAppMessages.wamid == "wamid.PIC"))
    h = await impersonating(session, make_admin, world)
    page = (await client.get(f"{API}/clubs/whatsapp/account", headers=h)).json()
    assert page["connected"] is True and page["can_inbox"] is False and page["impersonated"] is True
    assert (await client.get(f"{API}/clubs/whatsapp/account", headers=world.h)).json()["impersonated"] is False
    monkeypatch.setattr(settings, "LIVE_SSE", True)
    base = f"{API}/clubs/whatsapp"
    every_message_route = [
        ("GET", "/conversations", None),
        ("GET", "/conversations?q=Ananya", None),
        ("GET", f"/conversations/{conv.id}/messages", None),
        ("GET", f"/media/{picture.id}", None),
        ("GET", "/stream", None),
        ("GET", "/templates", None),
        ("POST", f"/conversations/{conv.id}/messages", {"type": "text", "text": "Hi, it's an admin"}),
        ("POST", f"/conversations/{conv.id}/read", None),
        ("PATCH", f"/conversations/{conv.id}", {"status": "closed"}),
        ("POST", "/conversations", {"registration_id": str(world.reg.id)}),
    ]
    for method, path, body in every_message_route:
        r = await client.request(method, f"{base}{path}", json=body, headers=h)
        assert r.status_code == 403 and r.json()["error_code"] == "WHATSAPP_PRIVATE", (method, path, r.text)
    assert meta.club_sent == [] and not meta.club_calls("GET", "/media-5")
    assert (await client.get(f"{API}/clubs/access/me", headers=h)).json()["can"]["whatsapp_inbox"] is False
    # Nor can the admin give a member the permission, and read through them.
    editor = await member(session, world, "editor")
    r = await client.put(f"{API}/clubs/access/{editor['_member_id']}/whatsapp", json={"allowed": True}, headers=h)
    assert r.status_code == 403 and r.json()["error_code"] == "WHATSAPP_PRIVATE"
    assert (await session.get(ClubMembers, int(editor["_member_id"]), populate_existing=True)).whatsapp is False


async def test_the_admin_portal_sees_status_and_quality_and_no_content(client, session, world, meta, make_admin, mint_token):
    account = await connected(client, session, world)
    await conversation_with(client, session, world, account, body="Very private words")
    admin = await make_admin()
    await session.commit()
    r = await client.get(f"{API}/admin/club-whatsapp/{world.club.id}", headers=auth_header(await mint_token(admin)))
    assert r.status_code == 200
    out = r.json()
    assert out["connected"] is True and out["status"] == "active" and out["quality_rating"] == "GREEN"
    assert out["display_phone_number"] == "+91 98470 12345" and out["conversations"] == 1 and out["plan_allows"] is True
    assert "Very private" not in r.text and TOKEN not in r.text and "verify_token" not in out
    assert (await client.get(f"{API}/admin/club-whatsapp/{world.club.id}", headers=world.h)).status_code == 403


def test_the_role_rules_for_whatsapp():
    account, inbox_path = "/api/v1/clubs/whatsapp/account", "/api/v1/clubs/whatsapp/conversations/1/messages"
    assert [policy.allows(r, "PUT", account) for r in ("admin", "editor", "door")] == [True, False, False]
    assert all(policy.allows(r, "GET", account) for r in ("admin", "editor", "door"))
    # The inbox reaches the route whatever the role: the permission decides there.
    assert all(policy.allows(r, "POST", inbox_path) for r in ("admin", "editor", "door"))
    assert [policy.can(r)["whatsapp_account"] for r in ("owner", "admin", "editor", "door")] == [True, True, False, False]


# ─── Retention and the live inbox ────────────────────────────────────────


async def test_retention_deletes_old_messages_and_empty_conversations(client, session, world, meta):
    account = await connected(client, session, world)
    now = datetime.now(timezone.utc)
    old = ClubWhatsAppConversations(club_id=world.club.id, account_id=account.id, business_phone_id=PHONE, wa_id="919000000002",
                                    last_message_at=now - timedelta(days=500), last_message_preview="old words")
    session.add(old)
    await session.flush()
    session.add(ClubWhatsAppMessages(conversation_id=old.id, club_id=world.club.id, direction="in", type="text", body="old words",
                                     status="received", created_at=now - timedelta(days=500)))
    conv = await conversation_with(client, session, world, account, body="new words")
    session.add(ClubWhatsAppMessages(conversation_id=conv.id, club_id=world.club.id, direction="in", type="text", body="a year ago",
                                     status="received", created_at=now - timedelta(days=400)))
    await session.commit()
    assert await sweep.retention(session, now) == 2
    left = (await session.scalars(select(ClubWhatsAppMessages.body))).all()
    assert left == ["new words"]
    assert await session.get(ClubWhatsAppConversations, old.id, populate_existing=True) is None
    # A shorter period, chosen by the club, deletes sooner.
    await client.patch(f"{API}/clubs/whatsapp/account", json={"retention_months": 1}, headers=world.h)
    assert await sweep.retention(session, now + timedelta(days=40)) == 1
    assert await session.scalar(select(func.count()).select_from(ClubWhatsAppMessages)) == 0


async def test_the_stream_is_off_unless_the_server_pushes(client, session, world, meta):
    await connected(client, session, world)
    r = await client.get(f"{API}/clubs/whatsapp/stream", headers=world.h)
    assert r.status_code == 404 and r.json()["error_code"] == "WHATSAPP_STREAM_OFF"


async def test_the_stream_names_the_conversation_never_its_words():
    stream = live.InboxStream(4242, lifetime=0.6)
    sent: list[dict] = []

    async def receive():
        await asyncio.sleep(30)
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    task = asyncio.create_task(stream({"type": "http"}, receive, send))
    await asyncio.sleep(0.05)
    await live.changed(4242, 7, "message")
    await live.changed(9999, 8, "message")  # another club's
    await asyncio.wait_for(task, 5)
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body").decode()
    assert "event: ready" in body and "event: changed" in body and '"conversation_id":7' in body
    assert '"conversation_id":8' not in body and "event: end" in body
