"""The team's support desk (app/api/support): the five doors, who may do
what, the thread with the person, the timers, the survey, the lists.

Mail, pushes and the bucket are in memory (tests/support_desk_helpers.py);
jobs run inline after each request, as the API's runner would."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.api.audit.models import AdminAuditLogs
from app.api.support import cases, hours, text
from app.api.support.models import SupportAttachments, SupportCases, SupportMessages
from app.core import jobs
from app.core.auth.permissions import MANAGE_MONEY, MODERATE, can, in_team
from tests.spreadsheet_helpers import records, workbook_of
from tests.support_desk_helpers import (  # noqa: F401 - fixtures
    API,
    PDF,
    PNG,
    _reference_fresh,
    ask,
    desk,
    desk_files,
    outbox,
    pushes,
    reply,
)

NOW = datetime.now(timezone.utc)


async def case_of(session, case_id) -> SupportCases:
    return await session.scalar(select(SupportCases).where(SupportCases.id == case_id).execution_options(populate_existing=True))






# ─── Teams sit on the roles ──────────────────────────────────────────────


async def test_team_membership_is_the_whole_grant(session, desk):
    assert await in_team(session, desk.sup, "support") and not await in_team(session, desk.sup, "sales")
    # A viewer in the support team is still a viewer everywhere else.
    assert not can(desk.sup, MODERATE) and not can(desk.sup, MANAGE_MONEY)
    assert not await in_team(session, desk.owner, "support")


async def test_only_owners_change_teams_and_only_admins_join(client, session, desk):
    r = await client.put(f"{API}/admin/teams/support/members", json={"user_ids": [desk.sup.id]}, headers=desk.h["sup"])
    assert r.status_code == 403 and r.json()["error_code"] == "INSUFFICIENT_ADMIN_ROLE"
    r = await client.put(f"{API}/admin/teams/support/members", json={"user_ids": [desk.person.id]}, headers=desk.h["owner"])
    assert r.status_code == 422 and r.json()["error_code"] == "NOT_AN_ADMIN"
    listed = await client.get(f"{API}/admin/teams", headers=desk.h["outsider"])
    assert listed.status_code == 200
    support = next(t for t in listed.json() if t["key"] == "support")
    assert {m["user_id"] for m in support["members"]} == {desk.sup.id, desk.sup2.id}
    eligible = await client.get(f"{API}/admin/teams/eligible", headers=desk.h["owner"])
    assert any(e["user_id"] == desk.outsider.id and e["teams"] == [] for e in eligible.json())


async def test_away_skips_round_robin_and_hands_cases_back(client, session, desk):
    first = (await ask(client, email="a1@example.org")).json()
    assert (await case_of(session, first["id"])).assignee_id == desk.sup.id
    r = await client.patch(f"{API}/admin/teams/me", json={"away": True}, headers=desk.h["sup"])
    assert r.status_code == 200
    assert (await case_of(session, first["id"])).assignee_id is None
    for n in range(2):
        made = (await ask(client, email=f"b{n}@example.org", subject=f"Other thing {n}")).json()
        assert (await case_of(session, made["id"])).assignee_id == desk.sup2.id
    r = await client.patch(f"{API}/admin/teams/me", json={"away": True}, headers=desk.h["outsider"])
    assert r.status_code == 404 and r.json()["error_code"] == "NOT_IN_A_TEAM"


# ─── The doors ───────────────────────────────────────────────────────────


async def test_a_guest_asks_with_screenshots_and_gets_a_receipt(client, session, desk):
    up = await client.post(f"{API}/support/uploads", files=[("files", ("shot.png", PNG, "image/png")), ("files", ("bill.pdf", PDF, "application/pdf"))])
    assert up.status_code == 201, up.text
    keys = [f["key"] for f in up.json()["files"]]
    r = await ask(client, attachments=keys, message="Card 4111 1111 1111 1111 was charged twice",
                  context={"platform": "web", "page_url": "https://myotherapp.in/events/x", "error_code": "PAYMENT_FAILED", "track_id": "t-1"})
    assert r.status_code == 201, r.text
    body = r.json()
    case = await case_of(session, body["id"])
    assert body["number"] == case.number >= 1001 and body["status"] == "new"
    assert case.channel == "web" and case.requester_kind == "guest" and case.requester_email == "guest@example.org"
    assert case.context == {"platform": "web", "page_url": "https://myotherapp.in/events/x", "error_code": "PAYMENT_FAILED", "track_id": "t-1"}
    assert case.priority == "normal" and case.first_reply_due is not None and case.resolution_due > case.first_reply_due
    first = await session.scalar(select(SupportMessages).where(SupportMessages.case_id == case.id))
    assert "4111 1111 1111 1111" not in first.body_text and "**** **** **** 1111" in first.body_text
    files = (await session.scalars(select(SupportAttachments).where(SupportAttachments.case_id == case.id))).all()
    assert len(files) == 2 and all(f.message_id == first.id for f in files)
    receipt = desk.outbox.sent[-1]
    assert receipt["to"] == "guest@example.org" and receipt["subject"] == f"[#{case.number}] My ticket didn't arrive"
    assert "passwords, OTPs or card numbers" in receipt["text"]
    audit = await session.scalar(select(AdminAuditLogs).where(AdminAuditLogs.action == "support.case.opened"))
    assert audit is not None and "4111" not in str(audit.details)


async def test_a_guest_must_say_who_they_are_and_the_upload_is_checked(client, session, desk):
    r = await client.post(f"{API}/support/requests", json={"topic": "other", "message": "help"})
    assert r.status_code == 422 and r.json()["error_code"] == "CONTACT_REQUIRED"
    r = await ask(client, topic="nonsense")
    assert r.status_code == 422 and r.json()["error_code"] == "INVALID_TOPIC"
    bad = await client.post(f"{API}/support/uploads", files={"file": ("x.png", b"<svg onload=alert(1)>", "image/png")})
    assert bad.status_code == 415
    big = await client.post(f"{API}/support/uploads", files={"file": ("x.png", PNG + b"0" * (5 * 1024 * 1024), "image/png")})
    assert big.status_code == 413
    r = await ask(client, attachments=["su_" + "0" * 32])
    assert r.status_code == 422 and r.json()["error_code"] == "UNKNOWN_ATTACHMENT"


async def test_guests_are_limited_per_email_and_per_address(client, session, desk):
    # One address, from anywhere: five an hour.
    for n in range(5):
        assert (await ask(client, email="same@example.org", subject=f"Thing {n}", net="203.0.113.5")).status_code == 201
    r = await ask(client, email="Same@Example.org", subject="Thing 6", net="198.51.100.7")
    assert r.status_code == 429 and r.json()["error_code"] == "RATE_LIMITED"
    # One network, whoever writes: six an hour.
    for n in range(6):
        assert (await ask(client, email=f"p{n}@example.org", subject=f"Other {n}", net="192.0.2.9")).status_code == 201
    assert (await ask(client, email="p7@example.org", subject="One more", net="192.0.2.9")).status_code == 429


async def test_the_same_person_on_the_same_subject_within_the_hour_is_one_case(client, session, desk):
    a = (await ask(client, subject="Refund please")).json()
    b = (await ask(client, subject="RE: refund please", message="Any news?")).json()
    assert a["id"] == b["id"]
    assert await session.scalar(select(func.count()).select_from(SupportMessages).where(SupportMessages.case_id == a["id"])) == 2


async def test_an_app_user_and_a_club_are_linked_and_see_only_their_own(client, session, desk, make_club, mint_token):
    from tests.conftest import auth_header

    mine = (await ask(client, headers=desk.h["person"], context={"platform": "app", "app_version": "1.2.1"})).json()
    case = await case_of(session, mine["id"])
    assert case.requester_kind == "user" and case.requester_user_id == desk.person.id and case.channel == "app"
    assert case.links["user_id"] == desk.person.id
    owner, club = await make_club(name="Yukthi", email="yukthi-desk@example.com")
    club.contact_email = "hello@yukthi.example.org"
    await session.commit()
    ch = auth_header(await mint_token(owner))
    theirs = (await ask(client, headers=ch, topic="dashboard", subject="Can't publish")).json()
    c2 = await case_of(session, theirs["id"])
    assert c2.requester_kind == "club" and c2.requester_club_id == club.id and c2.channel == "dashboard"
    assert c2.requester_email == "hello@yukthi.example.org"
    assert [r["id"] for r in (await client.get(f"{API}/support/requests", headers=desk.h["person"])).json()] == [mine["id"]]
    assert [r["id"] for r in (await client.get(f"{API}/support/requests", headers=ch)).json()] == [theirs["id"]]
    other = await client.get(f"{API}/support/requests/{theirs['id']}", headers=desk.h["person"])
    assert other.status_code == 404


async def test_a_club_door_member_may_ask_for_help(client, session, desk, make_club):
    from app.api.auth.schemas import AuthTokenData
    from app.api.club_access.models import ClubMembers
    from app.core.auth.jwt import create_access_token
    from tests.conftest import auth_header

    owner, club = await make_club(name="Chess Circle", email="chess-desk@example.com")
    m = ClubMembers(club_id=club.id, email="door@chess.example.org", full_name="Dee Door", role="door", status="active")
    session.add(m)
    await session.commit()
    await session.refresh(owner)
    token = create_access_token(data=AuthTokenData(user_id=owner.id, token_type="access_token", token_version=owner.token_version,
                                                   member_id=m.id).model_dump())
    r = await ask(client, headers=auth_header(token), topic="dashboard", subject="Scanner")
    assert r.status_code == 201, r.text
    case = await case_of(session, r.json()["id"])
    assert case.requester_member_id == m.id and case.requester_name == "Dee Door" and case.requester_email == "door@chess.example.org"


async def test_logging_a_case_by_hand(client, session, desk):
    body = {"requester": {"email": "caller@example.org", "name": "Cal Caller"}, "topic": "payments", "subject": "Phoned about a refund",
            "message": "Called at 3pm, wants a refund for DevFest."}
    r = await client.post(f"{API}/admin/support/cases", json=body, headers=desk.h["outsider"])
    assert r.status_code == 403 and r.json()["error_code"] == "TEAM_REQUIRED"
    r = await client.post(f"{API}/admin/support/cases", json=body, headers=desk.h["sup2"])
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["channel"] == "manual" and out["assignee"]["id"] == desk.sup2.id and out["priority"] == "high"


# ─── Who may do what on a case ───────────────────────────────────────────


async def test_bodies_are_hidden_from_admins_outside_the_teams(client, session, desk):
    made = (await ask(client)).json()
    await reply(client, made["id"], desk.h["sup"], kind="note", body="Checking Razorpay.")
    outsider = (await client.get(f"{API}/admin/support/cases/{made['id']}", headers=desk.h["outsider"])).json()
    assert outsider["reads_bodies"] is False
    assert all(m["body_text"] is None and m["body_html"] is None and m["hidden"] for m in outsider["messages"])
    listed = await client.get(f"{API}/admin/support/cases?tab=all", headers=desk.h["outsider"])
    assert listed.status_code == 200 and listed.json()["total"] == 1 and listed.json()["reads_bodies"] is False
    assert (await client.get(f"{API}/admin/support/cases?tab=all&format=xlsx", headers=desk.h["outsider"])).status_code == 403
    for who in ("sup", "sales", "owner"):
        seen = (await client.get(f"{API}/admin/support/cases/{made['id']}", headers=desk.h[who])).json()
        assert [m["body_text"] for m in seen["messages"]] == ["I paid but got no ticket.", "Checking Razorpay."], who
    files = await session.scalar(select(SupportAttachments.id))
    assert files is None
    att = await client.get(f"{API}/admin/support/attachments/1", headers=desk.h["outsider"])
    assert att.status_code == 403


async def test_only_the_assignee_answers_others_take_or_note_owners_reassign(client, session, desk):
    made = (await ask(client)).json()
    cid = made["id"]
    assert (await case_of(session, cid)).assignee_id == desk.sup.id
    r = await reply(client, cid, desk.h["sup2"])
    assert r.status_code == 403 and r.json()["error_code"] == "NOT_ASSIGNED_TO_YOU"
    assert (await reply(client, cid, desk.h["sup2"], kind="note", body="I know this one")).status_code == 200
    r = await reply(client, cid, desk.h["sales"])
    assert r.status_code == 403 and r.json()["error_code"] == "TEAM_REQUIRED"
    r = await reply(client, cid, desk.h["outsider"], kind="note", body="x")
    assert r.status_code == 403
    r = await client.patch(f"{API}/admin/support/cases/{cid}", json={"assignee_id": desk.sup2.id}, headers=desk.h["sup"])
    assert r.status_code == 403 and r.json()["error_code"] == "OWNER_REQUIRED"
    r = await client.post(f"{API}/admin/support/cases/{cid}/take", headers=desk.h["sup2"])
    assert r.status_code == 409 and r.json()["error_code"] == "CASE_TAKEN"
    r = await client.patch(f"{API}/admin/support/cases/{cid}", json={"assignee_id": None}, headers=desk.h["owner"])
    assert r.status_code == 200 and r.json()["assignee"] is None
    r = await client.post(f"{API}/admin/support/cases/{cid}/take", headers=desk.h["sup2"])
    assert r.status_code == 200 and r.json()["assignee"]["id"] == desk.sup2.id
    assert (await reply(client, cid, desk.h["sup2"])).status_code == 200
    r = await client.patch(f"{API}/admin/support/cases/{cid}", json={"assignee_id": desk.sales.id}, headers=desk.h["owner"])
    assert r.status_code == 422 and r.json()["error_code"] == "NOT_IN_TEAM"
    actions = set((await session.scalars(select(AdminAuditLogs.action).where(AdminAuditLogs.entity_type == "support_case"))).all())
    assert {"support.case.opened", "support.case.note", "support.case.update", "support.case.take", "support.case.reply"} <= actions


# ─── The thread ──────────────────────────────────────────────────────────


async def test_a_reply_goes_by_mail_with_the_number_and_headers_and_a_push(client, session, desk):
    made = (await ask(client, headers=desk.h["person"], subject="Where is my ticket")).json()
    cid = made["id"]
    preview = await reply(client, cid, desk.h["sup"], body="Hi {name}, it's **resent**.", preview_only=True)
    assert preview.status_code == 200 and preview.json()["preview"] is True
    assert preview.json()["subject"] == f"Re: [#{made['number']}] Where is my ticket" and "<strong>resent</strong>" in preview.json()["html"]
    assert "Hi Asha," in preview.json()["text"] and preview.json()["will_push"] is True
    before = len(desk.outbox.sent)
    assert await session.scalar(select(func.count()).select_from(SupportMessages).where(SupportMessages.kind == "reply_out")) == 0
    r = await reply(client, cid, desk.h["sup"], body="Hi {name}, it's **resent**.")
    assert r.status_code == 200, r.text
    mail = desk.outbox.sent[before]
    assert mail["to"] == desk.person.email and mail["subject"] == f"Re: [#{made['number']}] Where is my ticket"
    assert mail["headers"]["Message-ID"].startswith(f"<support.{made['number']}.") and mail["key"] == f"support:{r.json()['id']}"
    msg = await session.get(SupportMessages, r.json()["id"])
    await session.refresh(msg)
    assert msg.delivery_status == "sent" and msg.provider_message_id and msg.alt_message_id.endswith("amazonses.com>")
    case = await case_of(session, cid)
    assert case.status == "waiting" and case.first_replied_at is not None and case.waiting_since is not None
    assert desk.pushes[-1]["user_id"] == desk.person.id
    assert desk.pushes[-1]["data"] == {"type": "support_reply", "case_id": str(cid), "number": str(made["number"])}
    # The person's view: the reply, no notes, unread until opened.
    await reply(client, cid, desk.h["sup"], kind="note", body="internal only")
    listed = (await client.get(f"{API}/support/requests", headers=desk.h["person"])).json()
    assert listed[0]["unread"] is True
    view = (await client.get(f"{API}/support/requests/{cid}", headers=desk.h["person"])).json()
    assert [m["kind"] for m in view["messages"]] == ["reply_in", "reply_out"]
    assert "internal only" not in str(view)
    assert view["messages"][1]["author_name"] == "Sam (MyOtherApp)"
    assert (await client.get(f"{API}/support/requests", headers=desk.h["person"])).json()[0]["unread"] is False
    # Their answer puts it back in Open.
    back = await client.post(f"{API}/support/requests/{cid}/replies", json={"message": "Got it, thanks"}, headers=desk.h["person"])
    assert back.status_code == 201 and back.json()["new_case"] is False
    assert (await case_of(session, cid)).status == "open"


async def test_a_refused_or_bounced_reply_flags_not_delivered_and_reopens(client, session, desk):
    from app.api.hooks import service as hooks
    from app.api.hooks.verdicts import Verdict
    from app.api.deliveries.models import DeliveryOutcome

    made = (await ask(client)).json()
    cid = made["id"]
    r = await reply(client, cid, desk.h["sup"])
    msg = await session.get(SupportMessages, r.json()["id"])
    await session.refresh(msg)
    assert (await case_of(session, cid)).status == "waiting"
    n = await hooks.record(session, msg.provider_message_id, "email", Verdict(DeliveryOutcome.bounced, "Mailbox does not exist", "5.1.1"),
                           NOW, address="guest@example.org")
    assert n >= 1
    case = await case_of(session, cid)
    await session.refresh(msg)
    assert msg.delivery_status == "bounced" and "not_delivered" in case.flags and case.status == "open"
    lines = (await session.scalars(select(SupportMessages.body_text).where(SupportMessages.case_id == cid, SupportMessages.kind == "system"))).all()
    assert any("another way" in (t or "") for t in lines)
    # The address is now suppressed: the next reply is not sent, and says why.
    r = await reply(client, cid, desk.h["sup"], body="Trying again")
    m2 = await session.get(SupportMessages, r.json()["id"])
    await session.refresh(m2)
    assert m2.delivery_status == "failed" and "Not sent" in m2.delivery_error


async def test_reopen_within_7_days_of_resolved_and_a_new_linked_case_after(client, session, desk):
    made = (await ask(client, headers=desk.h["person"])).json()
    cid = made["id"]
    await reply(client, cid, desk.h["sup"])
    r = await client.patch(f"{API}/admin/support/cases/{cid}", json={"status": "resolved"}, headers=desk.h["sup"])
    assert r.status_code == 200 and r.json()["status"] == "resolved"
    survey = desk.outbox.sent[-1]
    case = await case_of(session, cid)
    assert case.survey_sent_at is not None and f"/help/survey/{case.survey_token}?solved=yes" in survey["text"]
    again = await client.post(f"{API}/support/requests/{cid}/replies", json={"message": "Still broken"}, headers=desk.h["person"])
    assert again.json()["new_case"] is False and (await case_of(session, cid)).status == "open"
    await client.patch(f"{API}/admin/support/cases/{cid}", json={"status": "resolved"}, headers=desk.h["sup"])
    case = await case_of(session, cid)
    case.resolved_at = NOW - timedelta(days=8)
    await session.commit()
    later = await client.post(f"{API}/support/requests/{cid}/replies", json={"message": "It broke again"}, headers=desk.h["person"])
    out = later.json()
    assert out["new_case"] is True and out["case_id"] != cid
    new = await case_of(session, out["case_id"])
    assert new.parent_case_id == cid and new.requester_user_id == desk.person.id and new.status == "new"
    closed = await case_of(session, cid)
    closed.status = "closed"
    await session.commit()
    third = await client.post(f"{API}/support/requests/{cid}/replies", json={"message": "And again"}, headers=desk.h["person"])
    assert third.json()["new_case"] is True


async def test_the_survey_by_its_link_and_signed_in(client, session, desk):
    made = (await ask(client, headers=desk.h["person"])).json()
    cid = made["id"]
    early = await client.post(f"{API}/support/requests/{cid}/survey", json={"solved": True}, headers=desk.h["person"])
    assert early.status_code == 409
    await client.patch(f"{API}/admin/support/cases/{cid}", json={"status": "resolved"}, headers=desk.h["sup"])
    token = (await case_of(session, cid)).survey_token
    assert (await client.post(f"{API}/support/survey/nope", json={"solved": True})).status_code == 404
    r = await client.post(f"{API}/support/survey/{token}", json={"solved": False, "comment": "still no ticket"})
    assert r.status_code == 200 and r.json()["status"] == "open"
    case = await case_of(session, cid)
    assert case.survey_solved is False and case.survey_comment == "still no ticket"
    view = (await client.get(f"{API}/support/requests/{cid}", headers=desk.h["person"])).json()
    assert view["survey"] == {"solved": False, "comment": "still no ticket"}


async def test_merge_moves_the_thread_and_closes_the_other(client, session, desk):
    a = (await ask(client, email="m@example.org", subject="First")).json()
    b = (await ask(client, email="m@example.org", subject="Second")).json()
    owner = desk.h["owner"]
    r = await client.post(f"{API}/admin/support/cases/{b['id']}/merge", json={"into_id": a["id"]}, headers=owner)
    assert r.status_code == 200 and r.json()["id"] == a["id"]
    src = await case_of(session, b["id"])
    assert src.status == "closed" and src.close_reason == "merged" and src.merged_into_id == a["id"]
    kinds = (await session.scalars(select(SupportMessages.kind).where(SupportMessages.case_id == a["id"]))).all()
    assert kinds.count("reply_in") == 2
    r = await client.post(f"{API}/admin/support/cases/{a['id']}/merge", json={"into_id": a["id"]}, headers=owner)
    assert r.status_code == 422


async def test_the_queue_tabs_counts_filters_and_excel(client, session, desk):
    one = (await ask(client, email="q1@example.org", subject="Login broken", topic="account")).json()
    two = (await ask(client, email="q2@example.org", subject="Refund", topic="payments")).json()
    await reply(client, one["id"], desk.h["sup"])
    r = (await client.get(f"{API}/admin/support/cases?tab=mine", headers=desk.h["sup"])).json()
    assert [i["id"] for i in r["items"]] == [one["id"]]
    assert r["counts"]["waiting"] == 1 and r["counts"]["open"] == 1
    item = (await client.get(f"{API}/admin/support/cases?tab=open", headers=desk.h["sup"])).json()["items"][0]
    assert item["id"] == two["id"] and item["priority"] == "high" and item["requester"] == {
        "kind": "guest", "id": None, "name": "Gita Guest", "email": "q2@example.org"}
    assert (await client.get(f"{API}/admin/support/cases?tab=all&topic=account", headers=desk.h["sup"])).json()["total"] == 1
    assert (await client.get(f"{API}/admin/support/cases?tab=all&q=%23{two['number']}", headers=desk.h["sup"])).json()["total"] == 1
    assert (await client.get(f"{API}/admin/support/cases?tab=nope", headers=desk.h["sup"])).status_code == 422
    sheet = workbook_of(await client.get(f"{API}/admin/support/cases?tab=all&format=xlsx", headers=desk.h["sup"])).active
    rows = records(sheet)
    assert {r["Number"] for r in rows} == {f"#{one['number']}", f"#{two['number']}"}
    assert "I paid" not in str(rows)


async def test_overdue_and_the_timers(client, session, desk):
    made = (await ask(client)).json()
    case = await case_of(session, made["id"])
    case.first_reply_due = NOW - timedelta(minutes=1)
    await session.commit()
    row = (await client.get(f"{API}/admin/support/cases?tab=open", headers=desk.h["sup"])).json()["items"][0]
    assert row["overdue"] is True
    assert cases.is_overdue(case, NOW) is True


async def test_case_to_lead_and_the_panel(client, session, desk, make_event, make_registration):
    made = (await ask(client, headers=desk.h["person"], subject="We want to sponsor")).json()
    detail = (await client.get(f"{API}/admin/support/cases/{made['id']}", headers=desk.h["sup"])).json()
    assert detail["panel"]["account"]["id"] == desk.person.id and detail["panel"]["account"]["path"] == f"/users/{desk.person.id}"
    assert detail["links"]["user_id"] == desk.person.id
    r = await client.post(f"{API}/admin/support/cases/{made['id']}/to-lead", json={"type": "sponsor", "name": "Acme Foods"}, headers=desk.h["outsider"])
    assert r.status_code == 403
    r = await client.post(f"{API}/admin/support/cases/{made['id']}/to-lead", json={"type": "sponsor", "name": "Acme Foods"}, headers=desk.h["sup"])
    assert r.status_code == 201, r.text
    lead = r.json()
    assert lead["type"] == "sponsor" and lead["case_id"] == made["id"] and lead["owner"]["id"] in (desk.sales.id, desk.sales2.id)
    assert (await case_of(session, made["id"])).lead_id == lead["id"]
    again = await client.post(f"{API}/admin/support/cases/{made['id']}/to-lead", json={"type": "sponsor"}, headers=desk.h["sup"])
    assert again.status_code == 409


async def test_settings_topics_saved_replies_and_help_answers(client, session, desk):
    r = await client.put(f"{API}/admin/support/settings", json={"retention_days": 90}, headers=desk.h["sup"])
    assert r.status_code == 403
    r = await client.put(f"{API}/admin/support/settings", json={"retention_days": 90, "first_reply_target_hours": 2}, headers=desk.h["owner"])
    assert r.status_code == 200 and r.json()["retention_days"] == 90 and r.json()["working_hours"]["start"] == "09:30"
    bad = await client.put(f"{API}/admin/support/settings", json={"retention_days": 5}, headers=desk.h["owner"])
    assert bad.status_code == 422
    topics = (await client.get(f"{API}/support/topics")).json()
    assert [t["key"] for t in topics] == ["account", "tickets", "payments", "dashboard", "report", "bug", "other"]
    r = await client.patch(f"{API}/admin/support/topics/payments", json={"assign": "person", "assignee_id": desk.sup2.id}, headers=desk.h["owner"])
    assert r.status_code == 200
    made = (await ask(client, topic="payments", email="p@example.org")).json()
    assert (await case_of(session, made["id"])).assignee_id == desk.sup2.id
    r = await client.patch(f"{API}/admin/support/topics/payments", json={"assign": "person", "assignee_id": desk.sales.id}, headers=desk.h["owner"])
    assert r.status_code == 422
    gone = await client.delete(f"{API}/admin/support/topics/payments", headers=desk.h["owner"])
    assert gone.json()["archived"] is True
    assert "payments" not in [t["key"] for t in (await client.get(f"{API}/support/topics")).json()]
    saved = await client.post(f"{API}/admin/support/saved-replies", json={"title": "Resent", "body": "Hi {name}, resent #{number}."},
                              headers=desk.h["owner"])
    assert saved.status_code == 201
    case = (await ask(client, email="s@example.org", subject="Saved")).json()
    msg = await reply(client, case["id"], desk.h["sup"], body="", saved_reply_id=saved.json()["id"])
    assert msg.status_code == 200 and msg.json()["body_text"] == f"Hi Gita, resent #{case['number']}."
    a = await client.post(f"{API}/admin/support/help-answers", json={"question": "Where is my ticket?", "answer": "In the app.",
                                                                     "published": True, "topic": "tickets"}, headers=desk.h["owner"])
    assert a.status_code == 201
    await client.post(f"{API}/admin/support/help-answers", json={"question": "Draft?", "answer": "Not yet."}, headers=desk.h["owner"])
    assert [x["question"] for x in (await client.get(f"{API}/support/help")).json()] == ["Where is my ticket?"]


async def test_my_day_the_badge_and_the_reports(client, session, desk):
    made = (await ask(client)).json()
    fu = await client.post(f"{API}/admin/follow-ups", json={"case_id": made["id"], "kind": "call", "title": "Call back",
                                                            "due_at": (NOW + timedelta(minutes=5)).isoformat()}, headers=desk.h["sup"])
    assert fu.status_code == 201, fu.text
    late = await client.post(f"{API}/admin/follow-ups", json={"case_id": made["id"], "kind": "email", "title": "Late one",
                                                              "due_at": (NOW - timedelta(days=2)).isoformat()}, headers=desk.h["sup"])
    assert late.status_code == 201
    day = (await client.get(f"{API}/admin/teamwork/my-day", headers=desk.h["sup"])).json()
    assert [f["title"] for f in day["follow_ups_overdue"]] == ["Late one"]
    assert [c["id"] for c in day["cases_mine_open"]] == [made["id"]]
    badge = (await client.get(f"{API}/admin/teamwork/badge", headers=desk.h["sup"])).json()
    assert badge["overdue"] == 1 and badge["cases"] == 1
    await reply(client, made["id"], desk.h["sup"])
    rep = (await client.get(f"{API}/admin/teamwork/reports", headers=desk.h["outsider"])).json()
    assert rep["support"]["cases"] == 1 and rep["support"]["first_reply_median_h"] is not None
    assert rep["leads"]["pipeline"]["club"]["new"] == 0
    sheet = workbook_of(await client.get(f"{API}/admin/teamwork/reports?format=xlsx", headers=desk.h["owner"])).active
    assert sheet["A1"].value == "Support"


async def test_jobs_run_twice_send_once(client, session, desk):
    made = (await ask(client)).json()
    r = await reply(client, made["id"], desk.h["sup"])
    sent = len(desk.outbox.sent)
    from app.api.support import mail

    await mail._reply_job({"message_id": r.json()["id"]})
    case = await case_of(session, made["id"])
    await mail._survey_job({"case_id": case.id})  # not resolved: nothing
    assert len(desk.outbox.sent) == sent
    await client.patch(f"{API}/admin/support/cases/{made['id']}", json={"status": "resolved"}, headers=desk.h["sup"])
    sent = len(desk.outbox.sent)
    await mail._survey_job({"case_id": case.id})
    assert len(desk.outbox.sent) == sent
    await jobs.settle()


# ─── Words and hours (last: a first test that opens no session leaves the
# suite's engine on another event loop when this file runs alone) ───────


def test_card_numbers_are_masked_and_other_numbers_left():
    assert text.mask_cards("my card 4111 1111 1111 1111 was charged") == "my card **** **** **** 1111 was charged"
    assert text.mask_cards("4111-1111-1111-1111") == "**** **** **** 1111"
    # Not a card: fails the Luhn check, or a phone number, or an order id.
    assert text.mask_cards("ticket 4111111111111112") == "ticket 4111111111111112"
    assert text.mask_cards("call +91 98470 12345") == "call +91 98470 12345"


def test_subjects_carry_the_number_and_keep_one_re():
    assert text.mail_subject(1042, "Re: [#1042] Parking") == "Re: [#1042] Parking"
    assert text.mail_subject(1042, "Parking", reply=False) == "[#1042] Parking"
    assert text.number_in("Re: Fwd: [#1042] Parking") == 1042
    assert text.subject_key("RE: [#7] Parking   free?") == text.subject_key("parking free?")


def test_markdown_lite_escapes_everything_but_its_own_marks():
    html = text.markdown_lite("Hi **Asha**,\n\n- one\n- two\n\n<script>x</script> see https://myotherapp.in/help")
    assert "<strong>Asha</strong>" in html and "<li>one</li>" in html
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert '<a href="https://myotherapp.in/help">' in html


def test_working_hours_count_monday_to_saturday_0930_to_1830_ist():
    ist = timezone(timedelta(hours=5, minutes=30))
    # Saturday 18:00 IST + 4 working hours: half an hour on Saturday, 3.5 on Monday.
    sat = datetime(2026, 11, 14, 18, 0, tzinfo=ist)
    assert hours.add_working_hours(sat, 4).astimezone(ist) == datetime(2026, 11, 16, 13, 0, tzinfo=ist)
    # Before the day starts it counts from 09:30.
    early = datetime(2026, 11, 11, 7, 0, tzinfo=ist)
    assert hours.add_working_hours(early, 2).astimezone(ist) == datetime(2026, 11, 11, 11, 30, tzinfo=ist)
    with pytest.raises(Exception):
        hours.validate({"working_hours": {"days": [], "start": "09:30", "end": "18:30"}})


async def test_the_queue_sorts_by_a_column_either_way(client, session, desk):
    a = (await ask(client, email="s1@example.org", subject="Zebra crossing", topic="account")).json()
    b = (await ask(client, email="s2@example.org", subject="apple pay", topic="account")).json()
    url = f"{API}/admin/support/cases?tab=all"

    def ids(r):
        return [i["id"] for i in r.json()["items"]]

    assert ids(await client.get(f"{url}&sort=subject", headers=desk.h["sup"])) == [b["id"], a["id"]]
    assert ids(await client.get(f"{url}&sort=subject&order=desc", headers=desk.h["sup"])) == [a["id"], b["id"]]
    # Numbers: newest (highest) first unasked.
    assert ids(await client.get(f"{url}&sort=number", headers=desk.h["sup"])) == [b["id"], a["id"]]
    assert ids(await client.get(f"{url}&sort=number&order=asc", headers=desk.h["sup"])) == [a["id"], b["id"]]
    assert (await client.get(f"{url}&sort=height", headers=desk.h["sup"])).status_code == 422
    assert (await client.get(f"{url}&sort=subject&order=up", headers=desk.h["sup"])).status_code == 422
