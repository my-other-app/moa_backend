"""The club's WhatsApp inbox worked as a team (club_whatsapp_inbox).

Alerts (the bell, the mails, waiting too long), STOP and opt-outs, blocking,
sending files and replying to a message, assigning, notes and saved replies.
Meta is never reached: test_club_whatsapp's `meta` stands in for it, and
`wa` here adds what it lacks (the blocklist, uploads it can be asked about).
"""
import inspect
import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import func, select

from app.api.audit.models import AdminAuditLogs
from app.api.club_alerts.models import ClubAlerts
from app.api.club_whatsapp import alerts_inbox, graph, optout, outbound_media, sweep
from app.api.club_whatsapp.inbox_models import (
    ClubWhatsAppNotes,
    ClubWhatsAppOptOuts,
    ClubWhatsAppSavedReplies,
)
from app.api.club_whatsapp.models import ClubWhatsAppConversations, ClubWhatsAppMessages
from app.api.deliveries import service as deliveries
from app.api.deliveries.models import Deliveries, DeliveryChannel, DeliveryKind, DeliveryStatus
from app.core import after_commit
from app.core.email import email as email_mod
from tests.test_club_whatsapp import (  # noqa: F401 - the fixtures are used by name
    API,
    BSUID,
    GUEST,
    NOW,
    PHONE,
    WABA,
    _enforced,
    _h,
    _reminder,
    _tpl,
    by_bsuid,
    connected,
    hook,
    inbound,
    mails,
    member as _member,
    meta,
    platform_number,
    plans,
    text_msg,
    world,
)
from tests.test_plan_enforcement import impersonating


async def member(session, w, role, *, whatsapp_permission=False) -> dict:
    """test_club_whatsapp's member(), and the address they sign in with."""
    from app.api.club_access.models import ClubMembers

    h = await _member(session, w, role, whatsapp_permission=whatsapp_permission)
    row = await session.get(ClubMembers, int(h["_member_id"]))
    return {**h, "_email": row.email}

BASE = f"{API}/clubs/whatsapp"
JPEG = b"\xff\xd8\xff\xe0" + b"j" * 200
PNG = b"\x89PNG\r\n\x1a\n" + b"p" * 200
PDF = b"%PDF-1.4\n" + b"d" * 300
OPUS = b"OggS" + b"\x00" * 24 + b"OpusHead" + b"o" * 100
DEFAULT_STOP = "You won't get WhatsApp messages from Yukthi any more. Reply START if you change your mind."
DEFAULT_START = "You'll get WhatsApp messages from Yukthi again. Reply STOP any time to stop them."


@pytest.fixture
def wa(meta, monkeypatch):
    """What `meta` lacks: the blocklist (POST/DELETE block_users) and a
    record of each media upload; the rest is `meta`'s."""
    extra = SimpleNamespace(block_answers=[], blocks=[], uploads=[])

    def handler(request: httpx.Request) -> httpx.Response:
        path = re.sub(r"^/v\d+\.\d+/", "", request.url.path)
        if request.url.host == "graph.facebook.com" and path == f"{PHONE}/block_users":
            request.read()
            meta.calls.append(request)
            body = json.loads(request.content)
            extra.blocks.append((request.method, body))
            if extra.block_answers:
                status, answer = extra.block_answers.pop(0)
                return httpx.Response(status, json=answer)
            entry = body["block_users"][0]
            said = entry.get("user") or entry.get("user_id")
            done = "added_users" if request.method == "POST" else "removed_users"
            return httpx.Response(200, json={"messaging_product": "whatsapp", "block_users": {done: [{"input": said}]}})
        if request.url.host == "graph.facebook.com" and path == f"{PHONE}/media" and request.method == "POST":
            request.read()
            meta.calls.append(request)
            extra.uploads.append(request)
            return httpx.Response(200, json={"id": f"media-out-{len(extra.uploads)}"})
        return meta.handler(request)

    monkeypatch.setattr(graph, "_client", lambda timeout=None: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return extra


async def say(client, account, body: str, *, wamid: str | None = None, at: datetime | None = None, wa_id: str = GUEST,
              name: str = "Ananya") -> str:
    wamid = wamid or f"wamid.IN{uuid.uuid4().hex[:10]}"
    r = await hook(client, account.webhook_token, inbound(text_msg(wamid, body, at=at, wa=wa_id), name=name, wa=wa_id))
    assert r.status_code == 200 and r.json()["ok"] is True, r.text
    return wamid


async def conv_of(session, w, wa_id: str = GUEST) -> ClubWhatsAppConversations:
    return await session.scalar(
        select(ClubWhatsAppConversations).where(ClubWhatsAppConversations.club_id == w.club.id, ClubWhatsAppConversations.wa_id == wa_id)
        .execution_options(populate_existing=True)
    )


async def optouts(session, club_id) -> list[ClubWhatsAppOptOuts]:
    return list((await session.scalars(
        select(ClubWhatsAppOptOuts).where(ClubWhatsAppOptOuts.club_id == club_id).order_by(ClubWhatsAppOptOuts.id)
        .execution_options(populate_existing=True)
    )).all())


async def bell(client, headers) -> dict:
    r = await client.get(f"{API}/clubs/alerts", headers=_h(headers))
    assert r.status_code == 200, r.text
    return r.json()


def texts(meta) -> list[dict]:
    return [m for m in meta.club_sent if m.get("type") == "text"]


async def upload(client, headers, conv_id: int, content: bytes, *, filename: str = "photo.jpg", mime: str = "image/jpeg",
                 size: int | None = None, caption: str | None = None, reply_to: int | None = None) -> httpx.Response:
    data = {"size": str(len(content) if size is None else size)}
    if caption is not None:
        data["caption"] = caption
    if reply_to is not None:
        data["reply_to"] = str(reply_to)
    return await client.post(f"{BASE}/conversations/{conv_id}/media", data=data, files={"file": (filename, content, mime)},
                             headers=_h(headers))


# ─── STOP and START ──────────────────────────────────────────────────────
#
# First in the module, and async: see test_club_whatsapp's note.


async def test_stop_opts_them_out_and_is_answered_once_start_opts_them_back_in(client, session, world, meta):
    account = await connected(client, session, world)
    stop = await say(client, account, "STOP")
    rows = await optouts(session, world.club.id)
    assert [(r.wa_id, r.scope, r.source) for r in rows] == [(GUEST, "all", "keyword")]
    # Answered at once, as a reply to it, in the window it just opened.
    answer = texts(meta)[-1]
    assert answer["to"] == GUEST and answer["text"]["body"] == DEFAULT_STOP and answer["context"] == {"message_id": stop}
    kept = await session.scalar(select(ClubWhatsAppMessages).where(ClubWhatsAppMessages.via == "auto"))
    assert kept.context_wamid == stop and kept.status == "sent" and kept.sent_by_name == "Automatic reply"
    conv = await conv_of(session, world)
    item = (await client.get(f"{BASE}/conversations/{conv.id}/messages", headers=world.h)).json()
    assert item["conversation"]["opt_out"]["scope"] == "all" and item["conversation"]["opt_out"]["source"] == "keyword"
    assert item["items"][-1]["via"] == "auto" and item["items"][-1]["quoted"]["id"] == item["items"][0]["id"]
    # A second STOP changes nothing, and isn't answered again.
    await say(client, account, "stop.")
    assert len(texts(meta)) == 1 and len(await optouts(session, world.club.id)) == 1
    # START opts them back in, and says so.
    await say(client, account, "Start!")
    assert await optouts(session, world.club.id) == []
    assert texts(meta)[-1]["text"]["body"] == DEFAULT_START
    out = (await client.get(f"{BASE}/conversations/{conv.id}/messages", headers=world.h)).json()["conversation"]
    assert out["opt_out"] is None


@pytest.mark.parametrize("stop_word, start_word", [
    ("STOP", "START"), ("stop all", "subscribe"), ("  Unsubscribe  ", "OPT IN"), ("Opt Out!", "unstop?"), ("OPTOUT", "start."),
])
async def test_each_keyword_and_its_answer(client, session, world, meta, stop_word, start_word):
    account = await connected(client, session, world)
    await say(client, account, stop_word)
    assert [r.scope for r in await optouts(session, world.club.id)] == ["all"]
    assert texts(meta)[-1]["text"]["body"] == DEFAULT_STOP
    await say(client, account, start_word)
    assert await optouts(session, world.club.id) == []
    assert texts(meta)[-1]["text"]["body"] == DEFAULT_START


@pytest.mark.parametrize("said, word", [
    ("STOP", "stop"), ("stop all", "stop"), ("Stop  All!!", "stop"), (" optout ", "stop"), ("OPT OUT.", "stop"),
    ("unsubscribe?", "stop"), ("START", "start"), ("Subscribe", "start"), ("opt in", "start"), ("UNSTOP!", "start"),
    ("stop please", None), ("STOPPED", None), ("don't stop", None), ("start the event", None), ("", None), (None, None),
    ("stop।", "stop"),
])
def test_what_counts_as_a_keyword(said, word):
    assert optout.keyword(said) == word


async def test_the_club_writes_its_own_answers(client, session, world, meta):
    account = await connected(client, session, world)
    r = await client.patch(f"{BASE}/inbox-settings", json={"stop_reply": "Bye from {club}. Reply START to come back.",
                                                          "start_reply": "Welcome back to {club}!"}, headers=world.h)
    assert r.status_code == 200 and r.json()["stop_reply"] == "Bye from {club}. Reply START to come back."
    assert r.json()["stop_reply_default"] == DEFAULT_STOP
    await say(client, account, "stop")
    assert texts(meta)[-1]["text"]["body"] == "Bye from Yukthi. Reply START to come back."
    await say(client, account, "start")
    assert texts(meta)[-1]["text"]["body"] == "Welcome back to Yukthi!"
    # "" puts the default back; the audit says what changed, never the words.
    await client.patch(f"{BASE}/inbox-settings", json={"stop_reply": ""}, headers=world.h)
    await say(client, account, "stop")
    assert texts(meta)[-1]["text"]["body"] == DEFAULT_STOP
    audit = (await session.scalars(select(AdminAuditLogs).where(AdminAuditLogs.action == "club_whatsapp.inbox_settings"))).all()
    assert audit and all("Bye from" not in json.dumps(a.details) for a in audit)


async def test_no_answer_to_someone_the_club_blocked_or_when_the_number_cannot_send(client, session, world, meta, wa):
    account = await connected(client, session, world)
    await say(client, account, "hello")
    conv = await conv_of(session, world)
    assert (await client.post(f"{BASE}/conversations/{conv.id}/block", json={"local_only": True}, headers=world.h)).status_code == 200
    await say(client, account, "STOP")
    # Opted out all the same, but nothing is sent to them.
    assert [r.source for r in await optouts(session, world.club.id)] == ["keyword"] and texts(meta) == []


# ─── What an opt-out stops ───────────────────────────────────────────────


async def test_an_opt_out_stops_templates_and_starting_but_not_a_reply_in_their_window(client, session, world, meta):
    account = await connected(client, session, world)
    await say(client, account, "STOP")
    conv = await conv_of(session, world)
    url = f"{BASE}/conversations/{conv.id}/messages"
    r = await client.post(url, json={"type": "template", "template": "event_update", "params": {"body": ["A", "B"]}}, headers=world.h)
    assert r.status_code == 409 and r.json()["error_code"] == "OPTED_OUT"
    r = await client.post(url, json={"type": "text", "text": "Understood, you're off the list."}, headers=world.h)
    assert r.status_code == 200 and r.json()["status"] == "sent"
    r = await client.post(f"{BASE}/conversations", json={"registration_id": str(world.reg.id)}, headers=world.h)
    assert r.status_code == 409 and r.json()["error_code"] == "OPTED_OUT"
    # Once their window has closed, nothing at all.
    conv.last_inbound_at = datetime.now(timezone.utc) - timedelta(days=2)
    await session.commit()
    r = await client.post(url, json={"type": "text", "text": "Still there?"}, headers=world.h)
    assert r.status_code == 409 and r.json()["error_code"] == "WINDOW_CLOSED"


async def test_an_opted_out_guests_whatsapp_is_skipped_from_the_clubs_number(client, session, world, meta):
    account = await connected(client, session, world)
    await say(client, account, "STOP")
    sent = len(meta.club_sent)
    d = await _reminder(session, world)
    assert d.status == DeliveryStatus.skipped and d.error == "opted out of Yukthi on WhatsApp"
    assert len(meta.club_sent) == sent and meta.platform_sent == []


async def test_and_from_the_platforms_number_while_the_email_still_goes(client, session, world, meta, platform_number, monkeypatch):
    from app.core.layout.model import TicketLayoutFields, dump
    from app.core.layout.starters import ticket_starter

    account = await connected(client, session, world)
    await say(client, account, "unsubscribe")
    # Routing off: the club's tickets would go from the platform's number.
    await client.patch(f"{BASE}/account", json={"routing": False}, headers=world.h)
    st = ticket_starter("classic")
    await client.put(f"{API}/events/{world.event.id}/designs/ticket/layout",
                     json=dump(TicketLayoutFields(canvas=st["canvas"], layers=st["layers"], page=st["page"])), headers=world.h)
    emailed = []
    monkeypatch.setattr(email_mod, "send_email_with_attachment", lambda **kw: emailed.append(kw) or {"MessageId": "ses-1"})
    monkeypatch.setattr(email_mod, "send_email", lambda **kw: emailed.append(kw) or {"MessageId": "ses-2"})
    await deliveries.queue(session, world.event, DeliveryKind.ticket, registration_ids=[world.reg.id])
    rows = (await session.scalars(select(Deliveries).where(Deliveries.registration_id == world.reg.id))).all()
    by = {r.channel: r for r in rows}
    d = await deliveries.deliver(session, by[DeliveryChannel.whatsapp].id)
    assert d.status == DeliveryStatus.skipped and d.error == "opted out of Yukthi on WhatsApp" and meta.platform_sent == []
    e = await deliveries.deliver(session, by[DeliveryChannel.email].id)
    assert e.status == DeliveryStatus.sent and emailed and emailed[-1]["recipients"] == [world.reg.email]


async def test_whatsapps_marketing_setting_and_131050_stop_marketing_only(client, session, world, meta):
    meta.templates.append({**_tpl("big_sale", "Hi {{1}}, 20% off!"), "category": "MARKETING"})
    account = await connected(client, session, world)
    await say(client, account, "hi")
    conv = await conv_of(session, world)
    prefs = {"object": "whatsapp_business_account", "entry": [{"id": WABA, "changes": [{"field": "user_preferences", "value": {
        "messaging_product": "whatsapp", "metadata": {"phone_number_id": PHONE},
        "user_preferences": [{"wa_id": GUEST, "user_id": BSUID, "detail": "User requested to stop marketing messages",
                              "category": "marketing_messages", "value": "stop", "timestamp": int(NOW.timestamp())}]}}]}]}
    assert (await hook(client, account.webhook_token, prefs)).status_code == 200
    assert [(r.scope, r.source, r.bsuid) for r in await optouts(session, world.club.id)] == [("marketing", "user_preferences", BSUID)]
    url = f"{BASE}/conversations/{conv.id}/messages"
    r = await client.post(url, json={"type": "template", "template": "big_sale", "params": {"body": ["A"]}}, headers=world.h)
    assert r.status_code == 409 and r.json()["error_code"] == "OPTED_OUT_MARKETING"
    r = await client.post(url, json={"type": "template", "template": "event_update", "params": {"body": ["A", "B"]}}, headers=world.h)
    assert r.status_code == 200
    # Only they can undo it.
    r = await client.delete(f"{BASE}/conversations/{conv.id}/optout", params={"scope": "marketing"}, headers=world.h)
    assert r.status_code == 409 and r.json()["error_code"] == "OPTOUT_IS_THEIRS"
    prefs["entry"][0]["changes"][0]["value"]["user_preferences"][0]["value"] = "resume"
    await hook(client, account.webhook_token, prefs)
    assert await optouts(session, world.club.id) == []
    # Meta failing a marketing template with 131050 says the same.
    wamid = meta.club_sent and f"wamid.CLUB{len(meta.club_sent)}"
    await hook(client, account.webhook_token, inbound(statuses=[{
        "id": wamid, "status": "failed", "timestamp": str(int(NOW.timestamp())), "recipient_id": GUEST,
        "errors": [{"code": 131050, "title": "Unable to deliver the message: the user stopped marketing messages"}]}]))
    assert [(r.scope, r.source, r.wa_id) for r in await optouts(session, world.club.id)] == [("marketing", "meta_131050", GUEST)]
    assert "user_preferences" in (await client.get(f"{BASE}/account", headers=world.h)).json()["webhook_fields"]


async def test_user_preferences_for_another_number_of_the_app_are_not_this_clubs(client, session, world, meta):
    account = await connected(client, session, world)
    prefs = {"object": "whatsapp_business_account", "entry": [{"id": WABA, "changes": [{"field": "user_preferences", "value": {
        "metadata": {"phone_number_id": "999999999999"},
        "user_preferences": [{"wa_id": GUEST, "category": "marketing_messages", "value": "stop"}]}}]}]}
    await hook(client, account.webhook_token, prefs)
    assert await optouts(session, world.club.id) == []


async def test_the_team_opts_someone_out_on_their_word_audited_without_content(client, session, world, meta):
    account = await connected(client, session, world)
    await say(client, account, "Please take me off your list, private words 88")
    conv = await conv_of(session, world)
    r = await client.post(f"{BASE}/conversations/{conv.id}/optout", json={"scope": "all"}, headers=world.h)
    assert r.status_code == 200 and r.json()["opt_out"]["source"] == "team" and r.json()["opt_out"]["by"]
    assert await optout.blocked(session, world.club.id, wa_id=GUEST)
    r = await client.delete(f"{BASE}/conversations/{conv.id}/optout", headers=world.h)
    assert r.status_code == 200 and r.json()["opt_out"] is None
    actions = (await session.scalars(select(AdminAuditLogs).where(AdminAuditLogs.action.in_(["club_whatsapp.optout", "club_whatsapp.optin"])).order_by(AdminAuditLogs.id))).all()
    assert [a.action for a in actions] == ["club_whatsapp.optout", "club_whatsapp.optin"]
    assert all("private words" not in json.dumps(a.details) + (a.summary or "") for a in actions)


async def test_the_contract_the_broadcasts_read(client, session, world, meta, wa):
    """optout.blocked(session, club_id, *, wa_id=None, bsuid=None, scope="all"|"marketing") -> bool."""
    params = inspect.signature(optout.blocked).parameters
    assert list(params) == ["session", "club_id", "wa_id", "bsuid", "scope"]
    assert params["wa_id"].kind is inspect.Parameter.KEYWORD_ONLY and params["scope"].default == "all"
    account = await connected(client, session, world)
    club = world.club.id
    assert not await optout.blocked(session, club, wa_id=GUEST) and not await optout.blocked(session, club)
    await optout.set_optout(session, club, wa_id=GUEST, bsuid=None, scope="marketing", source="user_preferences")
    # A marketing stop stops marketing only; an `all` one stops both; a number as typed is read too.
    assert await optout.blocked(session, club, wa_id=GUEST, scope="marketing")
    assert not await optout.blocked(session, club, wa_id=GUEST, scope="all")
    await optout.set_optout(session, club, wa_id=None, bsuid=BSUID, scope="all", source="keyword")
    assert await optout.blocked(session, club, bsuid=BSUID) and await optout.blocked(session, club, bsuid=BSUID, scope="marketing")
    assert await optout.blocked(session, club, wa_id="+91 98765 43210", scope="marketing")
    assert await optout.blocked(session, club, wa_id="98765 43210", scope="marketing")
    # Someone the club blocked in its inbox, too.
    await say(client, account, "hello", wa_id="919000000001", name="Bala")
    other = await conv_of(session, world, "919000000001")
    await client.post(f"{BASE}/conversations/{other.id}/block", json={"local_only": True}, headers=world.h)
    assert await optout.blocked(session, club, wa_id="919000000001")
    # Another club's opt-outs are its own.
    assert not await optout.blocked(session, world.other_club.id, bsuid=BSUID)
    with pytest.raises(ValueError):
        await optout.blocked(session, club, wa_id=GUEST, scope="everything")


# ─── Blocking ────────────────────────────────────────────────────────────


async def test_blocking_within_24_hours_goes_to_meta(client, session, world, meta, wa):
    account = await connected(client, session, world)
    await say(client, account, "spam spam")
    conv = await conv_of(session, world)
    r = await client.post(f"{BASE}/conversations/{conv.id}/block", json={}, headers=world.h)
    assert r.status_code == 200, r.text
    assert r.json()["blocked"]["meta"] is True and r.json()["blocked"]["by"]
    assert wa.blocks[-1] == ("POST", {"messaging_product": "whatsapp", "block_users": [{"user": f"+{GUEST}"}]})
    # Out of the inbox, into Blocked; nothing goes to them; the bell forgets them.
    assert (await client.get(f"{BASE}/conversations", headers=world.h)).json()["total"] == 0
    listed = (await client.get(f"{BASE}/conversations", params={"filter": "blocked"}, headers=world.h)).json()
    assert listed["total"] == 1 and listed["unread_total"] == 0
    r = await client.post(f"{BASE}/conversations/{conv.id}/messages", json={"type": "text", "text": "hi"}, headers=world.h)
    assert r.status_code == 409 and r.json()["error_code"] == "CONTACT_BLOCKED"
    assert [a for a in (await bell(client, world.h))["items"] if a["kind"] == "whatsapp_message"] == []
    assert (await client.get(f"{BASE}/summary", headers=world.h)).json()["unread_total"] == 0
    r = await client.delete(f"{BASE}/conversations/{conv.id}/block", headers=world.h)
    assert r.status_code == 200 and r.json()["blocked"] is None and wa.blocks[-1][0] == "DELETE"
    actions = [a.action for a in (await session.scalars(select(AdminAuditLogs).where(AdminAuditLogs.action.like("club_whatsapp.%block")).order_by(AdminAuditLogs.id))).all()]
    assert actions == ["club_whatsapp.block", "club_whatsapp.unblock"]
    # Unread again, so back in the bell.
    assert [a["kind"] for a in (await bell(client, world.h))["items"]] == ["whatsapp_message"]


async def test_after_24_hours_meta_wont_block_and_a_local_block_is_offered(client, session, world, meta, wa):
    account = await connected(client, session, world)
    await say(client, account, "old message", at=datetime.now(timezone.utc) - timedelta(days=2))
    conv = await conv_of(session, world)
    r = await client.post(f"{BASE}/conversations/{conv.id}/block", json={}, headers=world.h)
    assert r.status_code == 409 and r.json()["error_code"] == "BLOCK_WINDOW_CLOSED" and r.json()["errors"]["local_available"] is True
    assert wa.blocks == []
    r = await client.post(f"{BASE}/conversations/{conv.id}/block", json={"local_only": True}, headers=world.h)
    assert r.status_code == 200 and r.json()["blocked"]["meta"] is False and wa.blocks == []
    # They write again: kept, but nobody is told, and nothing can be sent back.
    await say(client, account, "are you there?")
    assert [a for a in (await bell(client, world.h))["items"] if a["kind"] == "whatsapp_message"] == []
    r = await client.post(f"{BASE}/conversations/{conv.id}/messages", json={"type": "text", "text": "hi"}, headers=world.h)
    assert r.json()["error_code"] == "CONTACT_BLOCKED"
    assert await alerts_inbox.mail_unread(session, datetime.now(timezone.utc) + timedelta(hours=1)) == 0
    # Lifted here only: Meta was never asked.
    r = await client.delete(f"{BASE}/conversations/{conv.id}/block", headers=world.h)
    assert r.status_code == 200 and wa.blocks == []


@pytest.mark.parametrize("status, answer, code, http", [
    (400, {"block_users": {"failed_users": [{"input": f"+{GUEST}", "errors": [{"code": 131047, "message": "Re-engagement required",
                                                                                "error_data": {"details": "User has not messaged in the last 24 hours"}}]}]},
           "error": {"code": 139100, "message": "(#139100) Failed to block/unblock users"}}, "BLOCK_WINDOW_CLOSED", 409),
    (400, {"error": {"code": 139101, "message": "Blocklist limit reached"}}, "BLOCKLIST_FULL", 409),
    (500, {"error": {"code": 139103, "message": "Internal error"}}, "BLOCK_TRY_AGAIN", 503),
    (400, {"error": {"code": 139102, "message": "Blocklist concurrent update"}}, "BLOCK_TRY_AGAIN", 503),
    (400, {"block_users": {"failed_users": [{"input": f"+{GUEST}", "errors": [{"code": 131021, "message": "Self block"}]}]},
           "error": {"code": 139100}}, "BLOCK_REFUSED", 409),
])
async def test_metas_blocklist_errors_are_said(client, session, world, meta, wa, status, answer, code, http):
    account = await connected(client, session, world)
    await say(client, account, "hello")
    conv = await conv_of(session, world)
    wa.block_answers.append((status, answer))
    r = await client.post(f"{BASE}/conversations/{conv.id}/block", json={}, headers=world.h)
    assert r.status_code == http and r.json()["error_code"] == code, r.text
    assert (await conv_of(session, world)).blocked_at is None


async def test_a_partial_failure_that_blocked_ours_is_a_block(client, session, world, meta, wa):
    account = await connected(client, session, world)
    await say(client, account, "hello")
    conv = await conv_of(session, world)
    wa.block_answers.append((200, {"block_users": {"added_users": [{"input": f"+{GUEST}", "wa_id": GUEST}],
                                                   "failed_users": [{"input": "+15550001111", "errors": [{"code": 131047}]}]},
                                   "error": {"code": 139100, "message": "(#139100) Failed to block/unblock users"}}))
    r = await client.post(f"{BASE}/conversations/{conv.id}/block", json={}, headers=world.h)
    assert r.status_code == 200 and r.json()["blocked"]["meta"] is True


async def test_someone_known_by_bsuid_is_blocked_by_it(client, session, world, meta, wa):
    account = await connected(client, session, world)
    await hook(client, account.webhook_token, by_bsuid("wamid.B1", "hi"))
    conv = await session.scalar(select(ClubWhatsAppConversations).where(ClubWhatsAppConversations.bsuid == BSUID))
    r = await client.post(f"{BASE}/conversations/{conv.id}/block", json={}, headers=world.h)
    assert r.status_code == 200 and wa.blocks[-1][1]["block_users"] == [{"user_id": BSUID}]


# ─── Sending files ───────────────────────────────────────────────────────


async def test_a_photo_is_streamed_to_meta_and_sent_with_its_caption(client, session, world, meta, wa):
    account = await connected(client, session, world)
    await say(client, account, "Can you send the map?")
    conv = await conv_of(session, world)
    r = await upload(client, world.h, conv.id, JPEG, caption="The hall is here.")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["type"] == "image" and out["body"] == "The hall is here." and out["status"] == "sent"
    assert out["media"]["url"] == f"/api/v1/clubs/whatsapp/media/{out['id']}" and out["media"]["mime"] == "image/jpeg"
    up = wa.uploads[0]
    assert up.headers["authorization"].startswith("Bearer ") and int(up.headers["content-length"]) == len(up.content)
    assert b'name="messaging_product"\r\n\r\nwhatsapp' in up.content and b'name="type"\r\n\r\nimage/jpeg' in up.content
    assert JPEG in up.content and b'filename="photo.jpg"' in up.content
    assert meta.club_sent[-1] == {"messaging_product": "whatsapp", "recipient_type": "individual", "to": GUEST, "type": "image",
                                  "image": {"id": "media-out-1", "caption": "The hall is here."}}
    kept = await session.get(ClubWhatsAppMessages, out["id"])
    assert kept.media_id == "media-out-1" and kept.direction == "out" and kept.via == "inbox"
    # Shown through the same proxy, with the same headers, as what arrives.
    shown = await client.get(f"{BASE}/media/{out['id']}", headers=world.h)
    assert shown.status_code == 200 and shown.headers["content-security-policy"] == "default-src 'none'; sandbox"
    assert shown.headers["x-content-type-options"] == "nosniff"
    # And it answers them: nothing waits any more.
    assert (await conv_of(session, world)).awaiting_since is None


async def test_a_document_keeps_its_name_and_sound_has_no_caption(client, session, world, meta, wa):
    account = await connected(client, session, world)
    await say(client, account, "Send the rules?")
    conv = await conv_of(session, world)
    r = await upload(client, world.h, conv.id, PDF, filename="Rules 2026.pdf", mime="application/pdf", caption="The rules")
    assert r.status_code == 200, r.text
    assert meta.club_sent[-1]["document"] == {"id": "media-out-1", "caption": "The rules", "filename": "Rules 2026.pdf"}
    assert r.json()["media"]["filename"] == "Rules 2026.pdf"
    r = await upload(client, world.h, conv.id, OPUS, filename="note.ogg", mime="audio/ogg")
    assert r.status_code == 200 and meta.club_sent[-1]["audio"] == {"id": "media-out-2"}
    r = await upload(client, world.h, conv.id, OPUS, filename="note.ogg", mime="audio/ogg", caption="listen")
    assert r.status_code == 422 and r.json()["error_code"] == "CAPTION_NOT_ALLOWED"
    # A type the browser didn't name is read from the file's name.
    r = await upload(client, world.h, conv.id, PNG, filename="map.png", mime="application/octet-stream")
    assert r.status_code == 200 and r.json()["media"]["mime"] == "image/png"


@pytest.mark.parametrize("content, filename, mime, size, caption, code, status", [
    (b"GIF89a" + b"g" * 50, "a.gif", "image/gif", None, None, "MEDIA_TYPE", 415),
    (b"<svg/>", "a.svg", "image/svg+xml", None, None, "MEDIA_TYPE", 415),
    (b"MZ" + b"x" * 50, "a.exe", "application/octet-stream", None, None, "MEDIA_TYPE", 415),
    (PNG, "fake.jpg", "image/jpeg", None, None, "MEDIA_TYPE", 415),
    (b"OggS" + b"\x00" * 60, "vorbis.ogg", "audio/ogg", None, None, "MEDIA_TYPE", 415),
    (JPEG, "big.jpg", "image/jpeg", 5_000_001, None, "MEDIA_TOO_LARGE", 413),
    (b"\x00\x00\x00\x18ftypmp42" + b"v" * 50, "big.mp4", "video/mp4", 16_000_001, None, "MEDIA_TOO_LARGE", 413),
    (PDF, "big.pdf", "application/pdf", 100_000_001, None, "MEDIA_TOO_LARGE", 413),
    (JPEG, "a.jpg", "image/jpeg", None, "x" * 1025, "CAPTION_TOO_LONG", 422),
    (JPEG, "a.jpg", "image/jpeg", len(JPEG) + 10, None, "MEDIA_SIZE_MISMATCH", 422),
    (JPEG, "a.jpg", "image/jpeg", len(JPEG) - 10, None, "MEDIA_SIZE_MISMATCH", 422),
])
async def test_a_file_whatsapp_wont_take_is_refused_before_meta_has_it(client, session, world, meta, wa, content, filename, mime, size,
                                                                        caption, code, status):
    account = await connected(client, session, world)
    await say(client, account, "hello")
    conv = await conv_of(session, world)
    r = await upload(client, world.h, conv.id, content, filename=filename, mime=mime, size=size, caption=caption)
    assert r.status_code == status and r.json()["error_code"] == code, r.text
    # A file that isn't what it says, or too large, never reaches Meta; nor is anything sent to them.
    if code != "MEDIA_SIZE_MISMATCH":
        assert wa.uploads == []
    assert [m for m in meta.club_sent if m.get("type") != "text"] == []


async def test_a_file_needs_its_size_and_the_window(client, session, world, meta, wa):
    account = await connected(client, session, world)
    await say(client, account, "hello", at=datetime.now(timezone.utc) - timedelta(days=2))
    conv = await conv_of(session, world)
    r = await upload(client, world.h, conv.id, JPEG)
    assert r.status_code == 409 and r.json()["error_code"] == "WINDOW_CLOSED" and wa.uploads == []
    await say(client, account, "hello again")
    conv = await conv_of(session, world)
    r = await client.post(f"{BASE}/conversations/{conv.id}/media", files={"file": ("a.jpg", JPEG, "image/jpeg")}, headers=world.h)
    assert r.status_code == 422 and r.json()["error_code"] == "MEDIA_SIZE_MISSING"
    r = await client.post(f"{BASE}/conversations/{conv.id}/media", data={"size": "10"}, headers=world.h)
    assert r.status_code == 422 and r.json()["error_code"] in ("MEDIA_MISSING", "INVALID_UPLOAD")
    r = await client.post(f"{BASE}/conversations/{conv.id}/media", json={"size": 10}, headers=world.h)
    assert r.status_code == 422 and r.json()["error_code"] == "INVALID_UPLOAD"
    assert wa.uploads == []


def test_metas_table_of_types_and_sizes():
    assert outbound_media.classify("image/jpeg", "a.jpg") == ("image", "image/jpeg", 5_000_000)
    assert outbound_media.classify("video/3gpp", "a.3gp")[0] == "video"
    assert outbound_media.classify("audio/ogg; codecs=opus", "a.ogg") == ("audio", "audio/ogg", 16_000_000)
    assert outbound_media.classify("", "a.m4a") == ("audio", "audio/mp4", 16_000_000)
    assert outbound_media.classify("application/vnd.openxmlformats-officedocument.presentationml.presentation", "a.pptx")[2] == 100_000_000
    assert outbound_media.classify("text/plain", "a.txt")[0] == "document"
    with pytest.raises(Exception):
        outbound_media.classify("image/webp", "a.webp")


# ─── Replying to a message ───────────────────────────────────────────────


async def test_a_reply_names_the_message_and_both_sides_show_the_quote(client, session, world, meta, wa):
    account = await connected(client, session, world)
    asked = await say(client, account, "Is parking free?")
    conv = await conv_of(session, world)
    theirs = await session.scalar(select(ClubWhatsAppMessages).where(ClubWhatsAppMessages.wamid == asked))
    url = f"{BASE}/conversations/{conv.id}/messages"
    r = await client.post(url, json={"type": "text", "text": "Yes, behind the hall.", "reply_to": theirs.id}, headers=world.h)
    assert r.status_code == 200, r.text
    assert meta.club_sent[-1]["context"] == {"message_id": asked}
    assert r.json()["context_wamid"] == asked and r.json()["quoted"] == {"id": theirs.id, "direction": "in", "type": "text",
                                                                        "preview": "Is parking free?"}
    ours = r.json()
    # They answer our message, quoting it: the inbox quotes it too.
    msg = text_msg("wamid.QUOTE", "Thanks!")
    msg["context"] = {"from": "919847012345", "id": ours["wamid"]}
    await hook(client, account.webhook_token, inbound(msg))
    items = {m["wamid"]: m for m in (await client.get(url, headers=world.h)).json()["items"]}
    assert items["wamid.QUOTE"]["quoted"]["id"] == ours["id"] and items["wamid.QUOTE"]["quoted"]["direction"] == "out"
    # A photo can answer too.
    r = await upload(client, world.h, conv.id, JPEG, reply_to=theirs.id)
    assert r.status_code == 200 and meta.club_sent[-1]["context"] == {"message_id": asked}
    # Not a message of another conversation, nor one without a WhatsApp id.
    await say(client, account, "hi", wa_id="919000000001", name="Bala")
    stranger = await session.scalar(select(ClubWhatsAppMessages).where(ClubWhatsAppMessages.body == "hi"))
    r = await client.post(url, json={"type": "text", "text": "x", "reply_to": stranger.id}, headers=world.h)
    assert r.status_code == 404 and r.json()["error_code"] == "MESSAGE_NOT_FOUND"


# ─── Alerts ──────────────────────────────────────────────────────────────


async def test_the_bell_tells_only_those_who_can_read_the_inbox(client, session, world, meta, make_admin):
    account = await connected(client, session, world)
    editor = await member(session, world, "editor")
    helper = await member(session, world, "door", whatsapp_permission=True)
    await say(client, account, "Very private words about the gate code")
    conv = await conv_of(session, world)
    for h in (world.h, helper):
        items = [a for a in (await bell(client, h))["items"] if a["kind"] == "whatsapp_message"]
        assert [(a["title"], a["link"]) for a in items] == [("Ananya Iyer: 1 unread WhatsApp message", f"/whatsapp?c={conv.id}")]
    seen = await bell(client, editor)
    assert seen["items"] == [] and seen["unread"] == 0
    admin_h = await impersonating(session, make_admin, world)
    assert (await bell(client, admin_h))["items"] == []
    # Marking everything read as someone who can't see them leaves them unread.
    await client.post(f"{API}/clubs/alerts/read", json={}, headers=_h(editor))
    assert (await bell(client, world.h))["unread"] == 1
    # One notice a conversation: a second message counts, and brings it back up.
    await say(client, account, "and another")
    items = [a for a in (await bell(client, world.h))["items"] if a["kind"] == "whatsapp_message"]
    assert len(items) == 1 and items[0]["title"] == "Ananya Iyer: 2 unread WhatsApp messages" and items[0]["data"]["count"] == 2
    assert "private" not in json.dumps(await bell(client, world.h))
    # Read: gone.
    await client.post(f"{BASE}/conversations/{conv.id}/read", headers=world.h)
    assert [a for a in (await bell(client, world.h))["items"] if a["kind"] == "whatsapp_message"] == []
    # Answered: gone too.
    await say(client, account, "one more")
    await client.post(f"{BASE}/conversations/{conv.id}/messages", json={"type": "text", "text": "ok"}, headers=world.h)
    assert [a for a in (await bell(client, world.h))["items"] if a["kind"] == "whatsapp_message"] == []


async def test_the_unread_badge_and_summary(client, session, world, meta):
    account = await connected(client, session, world)
    await say(client, account, "one")
    await say(client, account, "two")
    await say(client, account, "hi", wa_id="919000000001", name="Bala")
    out = (await client.get(f"{BASE}/summary", headers=world.h)).json()
    assert out == {"unread_total": 3, "unread_conversations": 2, "waiting": 2, "mine": 0}
    assert (await client.get(f"{BASE}/conversations", headers=world.h)).json()["unread_total"] == 3


async def test_a_mail_after_15_minutes_unread_at_most_hourly(client, session, world, meta, mails):
    account = await connected(client, session, world)
    helper = await member(session, world, "editor", whatsapp_permission=True)
    await member(session, world, "editor")
    await say(client, account, "The secret is 4417")
    now = datetime.now(timezone.utc)
    assert await alerts_inbox.mail_unread(session, now + timedelta(minutes=10)) == 0
    assert await alerts_inbox.mail_unread(session, now + timedelta(minutes=16)) == 2
    await after_commit.drain()
    unread = [m for m in mails if m["subject"] == "WhatsApp messages are waiting for Yukthi"]
    assert sorted(m["recipients"][0] for m in unread) == sorted(["yukthi@example.com", helper["_email"]])
    for m in unread:
        assert "Ananya Iyer (1)" in m["text"] and "4417" not in m["text"] + m["html"] + m["subject"] and "/whatsapp" in m["text"]
    # Nothing new: no mail; something new within the hour: not yet; after the hour: one more.
    assert await alerts_inbox.mail_unread(session, now + timedelta(minutes=40)) == 0
    await say(client, account, "hello?", wa_id="919000000001", name="Bala")
    # As if it came 45 minutes on (the clock here is the sweep's `now`).
    bala = await conv_of(session, world, "919000000001")
    bala.unread_since = now + timedelta(minutes=45)
    await session.commit()
    assert await alerts_inbox.mail_unread(session, now + timedelta(minutes=50)) == 0
    assert await alerts_inbox.mail_unread(session, now + timedelta(minutes=80)) == 2
    await after_commit.drain()
    assert "Bala (1)" in mails[-1]["text"] and "Ananya Iyer (1)" in mails[-1]["text"]
    # A person can turn them off.
    r = await client.patch(f"{BASE}/inbox-settings/me", json={"email_messages": False}, headers=_h(helper))
    assert r.status_code == 200 and r.json() == {"email_messages": False, "email_assigned": True}
    await say(client, account, "third", wa_id="919000000002", name="Chitra")
    chitra = await conv_of(session, world, "919000000002")
    chitra.unread_since = now + timedelta(minutes=140)
    await session.commit()
    assert await alerts_inbox.mail_unread(session, now + timedelta(minutes=160)) == 1


async def test_a_conversation_waiting_too_long_is_told_once_a_wait(client, session, world, meta, mails):
    account = await connected(client, session, world)
    r = await client.patch(f"{BASE}/inbox-settings", json={"alert_after_hours": 1}, headers=world.h)
    assert r.status_code == 200 and r.json()["alert_after_hours"] == 1
    assert (await client.patch(f"{BASE}/inbox-settings", json={"alert_after_hours": 49}, headers=world.h)).status_code == 422
    await say(client, account, "Where is the venue? Private: 77")
    conv = await conv_of(session, world)
    now = datetime.now(timezone.utc)
    assert await alerts_inbox.waiting_too_long(session, now + timedelta(minutes=30)) == 0
    assert await alerts_inbox.waiting_too_long(session, now + timedelta(minutes=61)) == 1
    assert await alerts_inbox.waiting_too_long(session, now + timedelta(minutes=90)) == 0
    await after_commit.drain()
    waiting = [m for m in mails if "waited" in m["subject"]]
    assert len(waiting) == 1 and "Ananya Iyer has waited" in waiting[0]["text"] and "77" not in waiting[0]["text"]
    notice = [a for a in (await bell(client, world.h))["items"] if a["kind"] == "whatsapp_waiting"]
    assert len(notice) == 1 and notice[0]["title"] == "Ananya Iyer has waited over 1 hour for an answer" and not notice[0]["read"]
    # An answer ends the wait (its notice read); a new message starts another.
    await client.post(f"{BASE}/conversations/{conv.id}/messages", json={"type": "text", "text": "Hall B"}, headers=world.h)
    assert [a["read"] for a in (await bell(client, world.h))["items"] if a["kind"] == "whatsapp_waiting"] == [True]
    await say(client, account, "thanks, and parking?")
    later = datetime.now(timezone.utc)
    assert await alerts_inbox.waiting_too_long(session, later + timedelta(minutes=61)) == 1
    # The default is 4 hours.
    assert (await client.get(f"{BASE}/inbox-settings", headers=world.h)).json()["alert_after_hours"] == 1


async def test_the_sweep_runs_the_alerts(client, session, world, meta, mails):
    account = await connected(client, session, world)
    await say(client, account, "hi")
    out = await sweep.sweep(session, datetime.now(timezone.utc) + timedelta(hours=5))
    assert out["alerts"]["waiting"] == 1 and out["alerts"]["mailed"] == 1


async def test_retention_takes_the_notes_and_the_bell_forgets(client, session, world, meta):
    account = await connected(client, session, world)
    await say(client, account, "hi", at=datetime.now(timezone.utc) - timedelta(days=500))
    conv = await conv_of(session, world)
    await client.post(f"{BASE}/conversations/{conv.id}/notes", json={"body": "VIP"}, headers=world.h)
    await session.execute(
        ClubWhatsAppMessages.__table__.update().values(created_at=datetime.now(timezone.utc) - timedelta(days=500))
    )
    await session.execute(
        ClubWhatsAppConversations.__table__.update().values(last_message_at=datetime.now(timezone.utc) - timedelta(days=500))
    )
    await session.commit()
    await sweep.retention(session)
    assert await session.scalar(select(func.count()).select_from(ClubWhatsAppNotes)) == 0
    assert await alerts_inbox.tidy(session) == 1
    assert [a for a in (await bell(client, world.h))["items"] if a["kind"] == "whatsapp_message"] == []


# ─── Assigning ───────────────────────────────────────────────────────────


async def test_assigning_to_someone_with_the_inbox_mails_them_and_filters(client, session, world, meta, mails):
    account = await connected(client, session, world)
    helper = await member(session, world, "editor", whatsapp_permission=True)
    plain = await member(session, world, "editor")
    await say(client, account, "Help with my ticket, private 99")
    await say(client, account, "hi", wa_id="919000000001", name="Bala")
    conv = await conv_of(session, world)
    team = (await client.get(f"{BASE}/team", headers=world.h)).json()
    assert team["me"] == "owner" and [p["key"] for p in team["items"]] == ["owner", f"member:{helper['_member_id']}"]
    url = f"{BASE}/conversations/{conv.id}/assignee"
    r = await client.put(url, json={"member_id": int(plain["_member_id"])}, headers=world.h)
    assert r.status_code == 422 and r.json()["error_code"] == "INVALID_ASSIGNEE"
    r = await client.put(url, json={"member_id": int(helper["_member_id"])}, headers=world.h)
    assert r.status_code == 200 and r.json()["assignee"]["kind"] == "member" and r.json()["assignee"]["name"] == "Pat Editor"
    await after_commit.drain()
    assigned = [m for m in mails if "assigned to you" in m["subject"]]
    assert len(assigned) == 1 and assigned[0]["recipients"] == [helper["_email"]]
    assert "Ananya Iyer" in assigned[0]["text"] and "private 99" not in assigned[0]["text"]
    audit = (await session.scalars(select(AdminAuditLogs).where(AdminAuditLogs.action == "club_whatsapp.assign"))).all()
    assert len(audit) == 1 and audit[0].details["to"] == f"member:{helper['_member_id']}"
    # Their "Mine", everyone's "Unassigned", and the person's filter.
    mine = (await client.get(f"{BASE}/conversations", params={"filter": "mine"}, headers=_h(helper))).json()
    assert [c["id"] for c in mine["items"]] == [conv.id]
    assert (await client.get(f"{BASE}/conversations", params={"filter": "mine"}, headers=world.h)).json()["total"] == 0
    assert (await client.get(f"{BASE}/conversations", params={"filter": "unassigned"}, headers=world.h)).json()["total"] == 1
    by = await client.get(f"{BASE}/conversations", params={"assignee": f"member:{helper['_member_id']}"}, headers=world.h)
    assert by.json()["total"] == 1
    assert (await client.get(f"{BASE}/summary", headers=_h(helper))).json()["mine"] == 1
    # To the owner (who assigns it to themselves: no mail), then to nobody.
    r = await client.put(url, json={"owner": True}, headers=world.h)
    assert r.json()["assignee"]["kind"] == "owner"
    assert (await client.get(f"{BASE}/conversations", params={"assignee": "owner"}, headers=world.h)).json()["total"] == 1
    r = await client.put(url, json={}, headers=world.h)
    assert r.json()["assignee"] is None
    assert (await client.put(url, json={"owner": True, "member_id": 5}, headers=world.h)).status_code == 422
    await after_commit.drain()
    assert len([m for m in mails if "assigned to you" in m["subject"]]) == 1
    # A member without the permission can't assign.
    assert (await client.put(url, json={"owner": True}, headers=_h(plain))).status_code == 403
    assert (await client.get(f"{BASE}/conversations", params={"filter": "nonsense"}, headers=world.h)).status_code == 422


async def test_an_assignee_can_turn_the_assignment_mail_off(client, session, world, meta, mails):
    account = await connected(client, session, world)
    helper = await member(session, world, "editor", whatsapp_permission=True)
    await client.patch(f"{BASE}/inbox-settings/me", json={"email_assigned": False}, headers=_h(helper))
    await say(client, account, "hi")
    conv = await conv_of(session, world)
    await client.put(f"{BASE}/conversations/{conv.id}/assignee", json={"member_id": int(helper["_member_id"])}, headers=world.h)
    await after_commit.drain()
    assert [m for m in mails if "assigned" in m["subject"]] == []
    me = (await client.get(f"{BASE}/inbox-settings", headers=_h(helper))).json()["me"]
    assert me == {"email_messages": True, "email_assigned": False}


# ─── Notes ───────────────────────────────────────────────────────────────


async def test_notes_are_the_teams_and_never_sent(client, session, world, meta):
    account = await connected(client, session, world)
    helper = await member(session, world, "editor", whatsapp_permission=True)
    other = await member(session, world, "door", whatsapp_permission=True)
    await say(client, account, "hi")
    conv = await conv_of(session, world)
    url = f"{BASE}/conversations/{conv.id}/notes"
    sent = len(meta.club_sent)
    r = await client.post(url, json={"body": "Sponsor's daughter: be kind, secret 55"}, headers=_h(helper))
    assert r.status_code == 200 and r.json()["author"] == "Pat Editor" and r.json()["mine"] is True
    note_id = r.json()["id"]
    assert len(meta.club_sent) == sent
    assert (await client.post(url, json={"body": "x" * 2001}, headers=world.h)).status_code == 422
    assert (await client.post(url, json={"body": "   "}, headers=world.h)).json()["error_code"] == "EMPTY_NOTE"
    listed = (await client.get(url, headers=_h(other))).json()["items"]
    assert len(listed) == 1 and listed[0]["mine"] is False and listed[0]["can_delete"] is False
    assert (await client.get(f"{BASE}/conversations/{conv.id}/messages", headers=world.h)).json()["conversation"]["notes"] == 1
    # Only its author, or the owner, deletes it.
    r = await client.delete(f"{url}/{note_id}", headers=_h(other))
    assert r.status_code == 403 and r.json()["error_code"] == "NOTE_NOT_YOURS"
    assert (await client.delete(f"{url}/{note_id}", headers=world.h)).json()["items"] == []
    # In the order they were written: without ORDER BY, Postgres returns rows in
    # whatever order the table's pages hold them, which turns on what earlier
    # tests wrote and rolled back and when autovacuum last reclaimed it.
    audit = (await session.scalars(select(AdminAuditLogs).where(AdminAuditLogs.action.like("club_whatsapp.note%")).order_by(AdminAuditLogs.id))).all()
    assert [a.action for a in audit] == ["club_whatsapp.note", "club_whatsapp.note_delete"]
    assert all("secret 55" not in json.dumps(a.details) + (a.summary or "") for a in audit)


# ─── Saved replies ───────────────────────────────────────────────────────


async def test_saved_replies(client, session, world, meta):
    await connected(client, session, world)
    helper = await member(session, world, "editor", whatsapp_permission=True)
    plain = await member(session, world, "editor")
    r = await client.post(f"{BASE}/saved-replies", json={"title": "Parking", "body": "Hi {name}, parking is free behind Hall B.",
                                                          "shortcut": "/Parking"}, headers=_h(helper))
    assert r.status_code == 200 and r.json()["shortcut"] == "parking"
    reply_id = r.json()["id"]
    r = await client.post(f"{BASE}/saved-replies", json={"title": "Again", "body": "x", "shortcut": "parking"}, headers=world.h)
    assert r.status_code == 409 and r.json()["error_code"] == "SHORTCUT_TAKEN"
    r = await client.post(f"{BASE}/saved-replies", json={"title": "Bad", "body": "x", "shortcut": "two words"}, headers=world.h)
    assert r.status_code == 422 and r.json()["errors"]["field"] == "shortcut"
    assert (await client.post(f"{BASE}/saved-replies", json={"title": "t" * 61, "body": "x"}, headers=world.h)).status_code == 422
    assert (await client.post(f"{BASE}/saved-replies", json={"title": "t", "body": "b" * 4097}, headers=world.h)).status_code == 422
    r = await client.put(f"{BASE}/saved-replies/{reply_id}", json={"title": "Parking", "body": "Behind Hall C now.", "shortcut": "parking"},
                         headers=world.h)
    assert r.status_code == 200 and r.json()["body"] == "Behind Hall C now."
    assert [x["title"] for x in (await client.get(f"{BASE}/saved-replies", headers=_h(helper))).json()["items"]] == ["Parking"]
    assert (await client.get(f"{BASE}/saved-replies", headers=_h(plain))).status_code == 403
    # Another club sees none of it.
    assert (await client.get(f"{BASE}/saved-replies", headers=world.other_h)).json()["items"] == []
    assert (await client.delete(f"{BASE}/saved-replies/{reply_id}", headers=world.other_h)).status_code == 404
    assert (await client.delete(f"{BASE}/saved-replies/{reply_id}", headers=world.h)).json()["items"] == []
    assert await session.scalar(select(func.count()).select_from(ClubWhatsAppSavedReplies)) == 0


# ─── Privacy and tenants ─────────────────────────────────────────────────


async def test_an_admin_acting_as_the_club_reaches_none_of_it(client, session, world, meta, wa, make_admin):
    account = await connected(client, session, world)
    await say(client, account, "hi")
    conv = await conv_of(session, world)
    note = (await client.post(f"{BASE}/conversations/{conv.id}/notes", json={"body": "n"}, headers=world.h)).json()
    saved = (await client.post(f"{BASE}/saved-replies", json={"title": "t", "body": "b"}, headers=world.h)).json()
    h = await impersonating(session, make_admin, world)
    c = f"/conversations/{conv.id}"
    routes = [
        ("GET", "/summary", None), ("GET", "/team", None), ("GET", "/inbox-settings", None),
        ("PATCH", "/inbox-settings", {"alert_after_hours": 2}), ("PATCH", "/inbox-settings/me", {"email_messages": False}),
        ("PUT", f"{c}/assignee", {"owner": True}), ("POST", f"{c}/block", {"local_only": True}), ("DELETE", f"{c}/block", None),
        ("POST", f"{c}/optout", {"scope": "all"}), ("DELETE", f"{c}/optout", None),
        ("GET", f"{c}/notes", None), ("POST", f"{c}/notes", {"body": "admin"}), ("DELETE", f"{c}/notes/{note['id']}", None),
        ("GET", "/saved-replies", None), ("POST", "/saved-replies", {"title": "t", "body": "b"}),
        ("PUT", f"/saved-replies/{saved['id']}", {"title": "t", "body": "b"}), ("DELETE", f"/saved-replies/{saved['id']}", None),
        ("GET", "/conversations?filter=blocked", None), ("GET", "/conversations?filter=mine", None),
    ]
    for method, path, body in routes:
        r = await client.request(method, f"{BASE}{path}", json=body, headers=h)
        assert r.status_code == 403 and r.json()["error_code"] == "WHATSAPP_PRIVATE", (method, path, r.text)
    r = await upload(client, h, conv.id, JPEG)
    assert r.status_code == 403 and r.json()["error_code"] == "WHATSAPP_PRIVATE"
    assert wa.uploads == [] and wa.blocks == []
    assert await session.scalar(select(func.count()).select_from(ClubWhatsAppNotes)) == 1


async def test_a_member_without_the_permission_reaches_none_of_it(client, session, world, meta):
    account = await connected(client, session, world)
    await say(client, account, "hi")
    conv = await conv_of(session, world)
    editor = _h(await member(session, world, "editor"))
    for method, path, body in [("GET", "/summary", None), ("GET", f"/conversations/{conv.id}/notes", None),
                               ("POST", f"/conversations/{conv.id}/block", {"local_only": True}), ("GET", "/inbox-settings", None)]:
        r = await client.request(method, f"{BASE}{path}", json=body, headers=editor)
        assert r.status_code == 403 and r.json()["error_code"] == "WHATSAPP_PERMISSION", (method, path)


async def test_another_club_cannot_touch_this_clubs_conversations(client, session, world, meta, wa, plans):
    from app.api.subscriptions import gates, service as subscriptions
    from app.api.subscriptions.models import BillingCycle

    account = await connected(client, session, world)
    await say(client, account, "hi")
    conv = await conv_of(session, world)
    await subscriptions.assign_plan(session, club_id=world.other_club.id, plan=plans["enterprise"], cycle=BillingCycle.monthly)
    await session.commit()
    gates.reset()
    c = f"/conversations/{conv.id}"
    for method, path, body in [
        ("PUT", f"{c}/assignee", {"owner": True}), ("POST", f"{c}/block", {"local_only": True}), ("DELETE", f"{c}/block", None),
        ("POST", f"{c}/optout", {"scope": "all"}), ("GET", f"{c}/notes", None), ("POST", f"{c}/notes", {"body": "x"}),
    ]:
        r = await client.request(method, f"{BASE}{path}", json=body, headers=world.other_h)
        assert r.status_code == 404 and r.json()["error_code"] == "CONVERSATION_NOT_FOUND", (method, path)
    r = await upload(client, world.other_h, conv.id, JPEG)
    assert r.status_code == 404 and wa.uploads == []
    # Its bell, summary and opt-outs are its own.
    assert (await bell(client, world.other_h))["items"] == []
    assert (await client.get(f"{BASE}/summary", headers=world.other_h)).json()["unread_total"] == 0
    await say(client, account, "STOP")
    assert not await optout.blocked(session, world.other_club.id, wa_id=GUEST)
    assert await session.scalar(select(func.count()).select_from(ClubAlerts).where(ClubAlerts.club_id == world.other_club.id)) == 0


async def test_no_message_text_reaches_the_log_audit_mail_or_bell(client, session, world, meta, wa, mails, caplog):
    secret = "my private words 5531"
    account = await connected(client, session, world)
    helper = await member(session, world, "editor", whatsapp_permission=True)
    with caplog.at_level(logging.DEBUG):
        await say(client, account, secret)
        await say(client, account, "STOP")
        conv = await conv_of(session, world)
        await client.post(f"{BASE}/conversations/{conv.id}/notes", json={"body": secret}, headers=world.h)
        await client.put(f"{BASE}/conversations/{conv.id}/assignee", json={"member_id": int(helper["_member_id"])}, headers=world.h)
        await client.post(f"{BASE}/conversations/{conv.id}/messages", json={"type": "text", "text": secret}, headers=world.h)
        await upload(client, world.h, conv.id, JPEG, caption=secret)
        await client.post(f"{BASE}/conversations/{conv.id}/block", json={}, headers=world.h)
        await say(client, account, secret, wa_id="919000000001", name="Bala")
        await alerts_inbox.run(session, datetime.now(timezone.utc) + timedelta(hours=6))
        await after_commit.drain()
    assert secret not in caplog.text
    audit = (await session.scalars(select(AdminAuditLogs).where(AdminAuditLogs.action.like("club_whatsapp.%")))).all()
    assert audit and all(secret not in json.dumps(a.details) + (a.summary or "") for a in audit)
    assert mails and all(secret not in m["subject"] + m["text"] + m["html"] for m in mails)
    notices = (await session.scalars(select(ClubAlerts))).all()
    assert notices and all(secret not in n.title + (n.body or "") + json.dumps(n.data or {}) for n in notices)
