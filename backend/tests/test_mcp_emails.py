"""An assistant customising an event's emails and the club's library over
MCP (app/api/mcp/email_tools.py).

Through the protocol: what each email is and when it goes; a change saved by
the dashboard's own save, its checks said in words, never switching an email
on; a preview and a test filled in for the sample guest; a test going only to
the person who connected, and only a few; the library; another club's event
"not found"; door staff reading but not changing; and sending a review email
again only through a person's approval.
"""
import uuid

import pytest
from sqlalchemy import select

from app.api.approvals.models import PENDING, RegistrationRequests
from app.api.audit.models import AdminAuditLogs
from app.api.emails.models import EmailStage, EmailTemplates, ReviewResends
from app.api.events.models import Events
from app.config import settings
from app.core.email import email as email_mod
from tests.conftest import auth_header
from tests.test_mcp import _owner_token, call, connect, mcp_on, rpc, tools_list, world  # noqa: F401

API = "/api/v1"
EVENT_TOOLS = {"list_event_emails", "get_event_email", "preview_event_email", "list_library_emails", "get_library_email", "preview_library_email"}
EMAIL_WRITES = {"update_event_email", "update_library_email", "send_test_email"}


@pytest.fixture
def outbox(monkeypatch):
    sent: list[dict] = []

    def send(**kwargs):
        sent.append(kwargs)
        return {"MessageId": f"ses-{len(sent)}"}

    monkeypatch.setattr(email_mod, "send_email", send)
    monkeypatch.setattr(email_mod, "send_email_with_attachment", lambda **kw: sent.append(kw))
    return sent


def _ok(out: dict) -> dict:
    assert "_error" not in out, out
    return out


# ─── Reading ─────────────────────────────────────────────────────────────


async def test_an_events_emails_and_what_each_is(client, world):
    c = await connect(client, "robotics@example.com", scopes=["emails:read"])
    assert set(await tools_list(client, c.access_token)) == EVENT_TOOLS
    listed = _ok(await call(client, c.access_token, "list_event_emails", {"event_id": world.ids.upcoming}))
    assert [e["key"] for e in listed["journey"]] == ["invite", "pre_event", "reminder", "post_event"]
    assert listed["journey"][0]["sends"].startswith("with each guest's ticket") and "before the event starts" in listed["journey"][1]["sends"]
    assert [e["key"] for e in listed["review"]["emails"]][:2] == ["request_received", "request_approved"]
    assert listed["review"]["in_use"] is False and listed["custom"] == []
    one = _ok(await call(client, c.access_token, "get_event_email", {"event_id": world.ids.upcoming, "key": "pre_event"}))
    assert one["group"] == "journey" and one["design"]["blocks"] and {"theme", "blocks"} <= set(one["design"])
    assert all(b["kind"] and b["id"] for b in one["design"]["blocks"])
    tokens = {t["key"] for t in one["tokens"]}
    assert {"attendee_name", "event_name", "ticket_url"} <= tokens and "pay_url" not in tokens
    pay = _ok(await call(client, c.access_token, "get_event_email", {"event_id": world.ids.upcoming, "key": "request_approved_pay"}))
    assert pay["group"] == "review" and pay["words"] == "MyOtherApp's words" and "pay_url" in {t["key"] for t in pay["tokens"]}
    assert (await call(client, c.access_token, "get_event_email", {"event_id": world.ids.upcoming, "key": "nope"}))["_error"] == "EMAIL_NOT_FOUND"
    assert (await call(client, c.access_token, "get_event_email", {"event_id": world.ids.upcoming, "key": f"custom:{uuid.uuid4()}"}))["_error"] == "EMAIL_NOT_FOUND"
    # Another club's event is not found.
    assert (await call(client, c.access_token, "list_event_emails", {"event_id": world.ids.secret}))["_error"] == "EVENT_NOT_FOUND"


async def test_door_staff_read_emails_but_neither_preview_nor_change_them(client, world):
    door = await connect(client, "door@robotics.example.com")
    names = set(await tools_list(client, door.access_token))
    assert {"list_event_emails", "get_event_email", "list_library_emails"} <= names and not (EMAIL_WRITES & names)
    assert "preview_event_email" not in names
    out = await call(client, door.access_token, "preview_event_email", {"event_id": world.ids.upcoming, "key": "invite"})
    assert out["_error"] == "ROLE_FORBIDDEN"
    r = await rpc(client, door.access_token, "tools/call", {"name": "update_event_email", "arguments": {"event_id": world.ids.upcoming, "key": "invite"}})
    assert r.status_code == 403 and 'scope="emails:write"' in r.headers["www-authenticate"]


# ─── Changing ────────────────────────────────────────────────────────────


async def test_changing_an_email_is_the_dashboards_save(client, session, world):
    c = await connect(client, "editor@robotics.example.com")
    args = {"event_id": world.ids.upcoming, "key": "pre_event"}
    before = _ok(await call(client, c.access_token, "get_event_email", args))
    blocks = [
        {"id": "h", "kind": "heading", "text": "Tomorrow, {{first_name}}!", "level": 1},
        {"id": "p", "kind": "paragraph", "text": "**{{event_name}}** starts {{event_start}}.", "style": {"align": "center"}},
        {"id": "b", "kind": "button", "label": "Your ticket", "href": "{{ticket_url}}", "shiny": True},
    ]
    saved = _ok(await call(client, c.access_token, "update_event_email", {**args, "subject": "See you tomorrow at {{event_name}}", "blocks": blocks,
                                                                     "theme": {"accent_text": "#000000", "font": "serif"}}))
    assert saved["saved"] and saved["changed"] == ["subject", "blocks", "theme"] and saved["version"] == before["version"] + 1
    assert [b["id"] for b in saved["design"]["blocks"]] == ["h", "p", "b"] and saved["design"]["theme"]["font"] == "serif"
    assert saved["design"]["theme"]["accentText"] == "#000000" and saved["design"]["blocks"][1]["style"] == {"align": "center"}
    assert saved["ignored"] == ["blocks.2 (button): shiny left out"]
    row = await session.scalar(select(EmailTemplates).where(EmailTemplates.event_id == world.ids.upcoming, EmailTemplates.stage == EmailStage.PRE_EVENT))
    await session.refresh(row)
    assert row.subject == "See you tomorrow at {{event_name}}"
    # Its checks, said in words; nothing saved.
    out = await call(client, c.access_token, "update_event_email", {**args, "subject": "Hi {{nope}}"})
    assert out["_error"] == "INVALID_EMAIL" and "subject" in out["message"]
    out = await call(client, c.access_token, "update_event_email", {**args, "blocks": [{"id": "x", "kind": "marquee", "text": "hi"}]})
    assert out["_error"] == "INVALID_EMAIL"
    out = await call(client, c.access_token, "update_event_email", {**args, "subject": "Pay {{pay_url}}"})
    assert out["_error"] == "INVALID_EMAIL" and "pay_url" in out["message"]
    await session.refresh(row)
    assert row.subject == "See you tomorrow at {{event_name}}"
    # Off, yes; on, only in the dashboard.
    assert _ok(await call(client, c.access_token, "update_event_email", {**args, "enabled": False}))["enabled"] is False
    on = await call(client, c.access_token, "update_event_email", {**args, "enabled": True})
    assert on["_error"] == "SWITCH_ON_IN_DASHBOARD" and on["dashboard_url"].endswith(f"/events/{world.ids.upcoming}/emails")
    await session.refresh(row)
    assert row.enabled is False
    # Its timer, audience and attachments are not this tool's.
    r = await call(client, c.access_token, "update_event_email", {**args, "trigger": {"mode": "BEFORE_START", "minutes": 5}})
    assert r["_error"] == "INVALID_ARGUMENTS"
    assert (await call(client, c.access_token, "update_event_email", args))["_error"] == "INVALID_ARGUMENT"
    audit = list(await session.scalars(select(AdminAuditLogs).where(AdminAuditLogs.action == "mcp.email.updated")))
    assert len(audit) == 2 and all(a.details["member_email"] == "editor@robotics.example.com" for a in audit)


async def test_a_review_email_gets_the_events_own_copy(client, session, world):
    c = await connect(client, "robotics@example.com")
    args = {"event_id": world.ids.upcoming, "key": "request_declined"}
    saved = _ok(await call(client, c.access_token, "update_event_email", {**args, "subject": "Not this time, {{first_name}}"}))
    assert saved["words"] == "this event's own words" and saved["enabled"] is True
    copy = await session.scalar(select(EmailTemplates).where(EmailTemplates.event_id == world.ids.upcoming, EmailTemplates.stage == EmailStage.REQUEST_DECLINED))
    assert copy is not None and copy.subject == "Not this time, {{first_name}}"
    # The club's library one is untouched; a review email is never switched off.
    lib = _ok(await call(client, c.access_token, "get_library_email", {"key": "request_declined"}))
    assert lib["subject"] != "Not this time, {{first_name}}"
    assert (await call(client, c.access_token, "update_event_email", {**args, "enabled": False}))["_error"] == "ALWAYS_ON"


async def test_the_library(client, session, world):
    c = await connect(client, "robotics@example.com")
    # The event's emails opened once: it has its own copies from then on.
    _ok(await call(client, c.access_token, "list_event_emails", {"event_id": world.ids.upcoming}))
    lib = _ok(await call(client, c.access_token, "list_library_emails", {}))
    keys = [e["key"] for e in lib["emails"]]
    assert keys[:4] == ["invite", "pre_event", "reminder", "post_event"] and "request_received" in keys
    saved = _ok(await call(client, c.access_token, "update_library_email", {"key": "post_event", "subject": "Thanks for coming to {{event_name}}"}))
    assert saved["subject"] == "Thanks for coming to {{event_name}}" and "/emails/" in saved["dashboard_url"]
    preview = _ok(await call(client, c.access_token, "preview_library_email", {"key": "post_event"}))
    assert preview["subject"] == "Thanks for coming to Sample event"
    # The event keeps its own copy; one that had not copied them yet starts from the library's.
    own = _ok(await call(client, c.access_token, "get_event_email", {"event_id": world.ids.upcoming, "key": "post_event"}))
    assert own["subject"] != "Thanks for coming to {{event_name}}"
    fresh = _ok(await call(client, c.access_token, "get_event_email", {"event_id": world.ids.past, "key": "post_event"}))
    assert fresh["subject"] == "Thanks for coming to {{event_name}}"
    # Another club's custom email is not found here.
    from app.api.emails import service
    from app.core.mail.starters import email_starter

    theirs = await service.create_template(session, club_id=world.ids.other_club, event_id=None, fields=email_starter("PRE_EVENT"))
    out = await call(client, c.access_token, "get_library_email", {"key": f"custom:{theirs.id}"})
    assert out["_error"] == "EMAIL_NOT_FOUND"


# ─── Previews and tests ──────────────────────────────────────────────────


async def test_a_preview_and_a_test_are_for_the_sample_guest(client, session, world, outbox):
    c = await connect(client, "editor@robotics.example.com")
    args = {"event_id": world.ids.upcoming, "key": "invite"}
    preview = _ok(await call(client, c.access_token, "preview_event_email", args))
    assert "Bot Build Night" in preview["subject"] + preview["text"] and "html" not in preview
    for real in ("Asha", "Ravi", "asha.k@example.com", "98765"):
        assert real not in preview["text"] + preview["subject"], real
    unsaved = _ok(await call(client, c.access_token, "preview_event_email", {**args, "subject": "Draft: {{event_name}}", "include_html": True}))
    assert unsaved["subject"] == "Draft: Bot Build Night" and unsaved["unsaved_changes"] and "<html" in unsaved["html"].lower()
    assert _ok(await call(client, c.access_token, "get_event_email", args))["subject"] != "Draft: {{event_name}}"

    # A test goes to the connected person, and only there; there is no address to give.
    sent = _ok(await call(client, c.access_token, "send_test_email", {**args, "subject": "Draft: {{event_name}}"}))
    assert sent["to"] == "editor@robotics.example.com" and sent["subject"] == "[Test] Draft: Bot Build Night"
    assert [m["recipients"] for m in outbox] == [["editor@robotics.example.com"]] and outbox[0]["subject"] == "[Test] Draft: Bot Build Night"
    assert "Asha" not in outbox[0]["text"]
    r = await call(client, c.access_token, "send_test_email", {**args, "to": "someone@example.com"})
    assert r["_error"] == "INVALID_ARGUMENTS" and len(outbox) == 1
    # The club account's own test goes to the club's address.
    owner = await connect(client, "robotics@example.com")
    assert _ok(await call(client, owner.access_token, "send_test_email", args))["to"] == "robotics@example.com"
    # A few, then a wait.
    assert _ok(await call(client, c.access_token, "send_test_email", args))["sent"]
    assert _ok(await call(client, c.access_token, "send_test_email", args))["sent"]
    limited = await call(client, c.access_token, "send_test_email", args)
    assert limited["_error"] == "TEST_EMAIL_LIMITED" and limited["retry_after"] > 0
    assert len(outbox) == 4
    audit = list(await session.scalars(select(AdminAuditLogs).where(AdminAuditLogs.action == "mcp.email.test_sent")))
    assert len(audit) == 4 and {a.details["to"] for a in audit} == {"editor@robotics.example.com", "robotics@example.com"}


async def test_a_broken_email_is_not_sent_as_a_test(client, world, outbox):
    c = await connect(client, "robotics@example.com")
    out = await call(client, c.access_token, "send_test_email", {"event_id": world.ids.upcoming, "key": "invite", "subject": "Hi {{nope}}"})
    assert out["_error"] == "INVALID_EMAIL" and outbox == []


# ─── Sending a review email again: asked for, approved ───────────────────


async def test_sending_a_review_email_again_waits_for_a_person(client, session, world, outbox, monkeypatch):
    from app.api.deliveries import service as deliveries

    monkeypatch.setattr(settings, "MCP_GUEST_REQUESTS_ENABLED", True)
    c = await connect(client, "robotics@example.com")
    args = {"event_id": world.ids.upcoming, "key": "request_received"}
    assert (await call(client, c.access_token, "request_resend_review_email", args))["_error"] == "NOT_REVIEWED"
    event = await session.scalar(select(Events).where(Events.id == world.ids.upcoming))
    event.requires_approval = True
    await session.commit()
    assert (await call(client, c.access_token, "request_resend_review_email", args))["_error"] == "NOBODY"
    assert (await call(client, c.access_token, "request_resend_review_email", {**args, "key": "request_unanswered"}))["_error"] == "NOT_RESENDABLE"
    session.add(RegistrationRequests(event_id=world.ids.upcoming, full_name="Waiting Wendy", email="wendy@example.com", status=PENDING, token="t" * 40))
    await session.commit()
    out = _ok(await call(client, c.access_token, "request_resend_review_email", args))
    assert out["status"] == "pending_approval" and out["kind"] == "resend_review_email" and out["would_reach"] == 1
    assert await session.scalar(select(ReviewResends).where(ReviewResends.event_id == world.ids.upcoming)) is None and outbox == []
    token = out["confirm_url"].rsplit("/", 1)[1]
    h = auth_header(await _owner_token(session, world))
    page = (await client.get(f"{API}/clubs/pending-actions/{token}", headers=h)).json()
    assert page["title"] == "Send “Thanks for registering” again" and page["can_decide"] is True
    assert page["target"]["email"]["eligible"] == 1 and page["target"]["id"] == world.ids.upcoming

    async def nothing(*_a, **_k):
        return 0

    monkeypatch.setattr(deliveries, "_run_due", nothing)
    done = await client.post(f"{API}/clubs/pending-actions/{token}/confirm", headers=h)
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "confirmed" and done.json()["result"]["people"] == 1
    resend = await session.scalar(select(ReviewResends).where(ReviewResends.event_id == world.ids.upcoming))
    assert resend is not None and resend.recipients == 1 and resend.stage == "REQUEST_RECEIVED"


async def test_a_resend_for_a_deleted_event_cannot_be_approved(client, session, world, monkeypatch):
    monkeypatch.setattr(settings, "MCP_GUEST_REQUESTS_ENABLED", True)
    c = await connect(client, "robotics@example.com")
    event = await session.scalar(select(Events).where(Events.id == world.ids.upcoming))
    event.requires_approval = True
    session.add(RegistrationRequests(event_id=world.ids.upcoming, full_name="Waiting Wendy", email="wendy@example.com", status=PENDING, token="t" * 40))
    await session.commit()
    out = _ok(await call(client, c.access_token, "request_resend_review_email", {"event_id": world.ids.upcoming, "key": "request_received"}))
    token = out["confirm_url"].rsplit("/", 1)[1]
    h = auth_header(await _owner_token(session, world))
    event.is_deleted = True
    await session.commit()
    page = (await client.get(f"{API}/clubs/pending-actions/{token}", headers=h)).json()
    # Soft-deleted rows are never read: the event is gone, and so is Approve.
    assert page["status"] == "pending" and page["target"] == {}
    assert page["can_decide"] is False
    assert page["cannot_decide_reason"] == "This action is no longer possible."
