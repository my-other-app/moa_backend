"""Missing answers, phases 2 and 3 (feature-register/missing-answers-contract.md
§2): asks and what they send, the personal link and answering by it, the
flags on tickets and lists, the door, and the club's own WhatsApp.

The form, and who is missing what:

    status   radio   [Student, Working]  required
    college  text                        required  show_if status in [Student]
    tshirt   select  [S, M, L]           required
    why      textarea                    optional

    app      Working, tshirt blank        an app account with a phone, WhatsApp ticked
    mailer   Student, college blank       an email address only
    done     Working, M                   nothing missing
    request  Working (a pending request)  tshirt blank
"""
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.api.approvals.models import RegistrationRequests
from app.api.events.models import EventCategories, Events
from app.api.missing_answers import asks as asks_mod
from app.api.missing_answers import people as people_mod
from app.api.missing_answers.models import AnswerAskPeople, AnswerAsks, AnswerAskSends
from app.api.users.models import UserDeviceTokens
from app.core import jobs
from app.core.rate_limit import limiter
from tests.conftest import auth_header

pytestmark = pytest.mark.usefixtures("session")

API = "/api/v1"
STUDENT = {"key": "status", "any_of": ["Student"]}


def q(key, label, kind, options=None, show_if=None, required=True):
    out = {"key": key, "label": label, "field_type": kind, "required": required, "options": options}
    if show_if is not None:
        out["show_if"] = show_if
    return out


FORM = [
    q("status", "Student or working?", "radio", ["Student", "Working"]),
    q("college", "College", "text", show_if=STUDENT),
    q("tshirt", "T-shirt size", "select", ["S", "M", "L"]),
    q("why", "Why are you coming?", "textarea", required=False),
]


class Sent:
    def __init__(self):
        self.mail, self.push, self.whatsapp, self.counted = [], [], [], []


@pytest.fixture
def sent(monkeypatch):
    from app.api.message_quota import service as quota
    from app.core import whatsapp
    from app.core.email import email as email_mod
    from app.core.notifications import service as push

    out = Sent()

    def send_email(**kw):
        out.mail.append(kw)
        return {"MessageId": f"mail-{len(out.mail)}", "Provider": "resend"}

    async def send_push(session, user_id, title, body, data=None):
        out.push.append({"user_id": user_id, "title": title, "body": body, "data": data})
        return True

    async def send_wa(number, template, **kw):
        out.whatsapp.append({"number": number, "template": template, **kw})
        return {"MessageId": f"wamid.{len(out.whatsapp)}", "Via": "club"}

    async def count(session, **kw):
        out.counted.append(kw)

    async def check(session, club_id, channel, kind, **kw):
        return None, None

    monkeypatch.setattr(email_mod, "send_email", send_email)
    monkeypatch.setattr(email_mod, "send_email_with_attachment", lambda **kw: None)
    monkeypatch.setattr(push, "send_notification_to_user", send_push)
    monkeypatch.setattr(whatsapp, "send", send_wa)
    monkeypatch.setattr(quota, "count", count)
    monkeypatch.setattr(quota, "check", check)
    monkeypatch.setattr(limiter, "enabled", False)
    return out


@pytest.fixture
async def world(session, make_club, make_event, make_user, make_registration, mint_token, sent):
    owner, club = await make_club(name="Ask Club", email="ask-club@example.com")
    category = EventCategories(name="Talks")
    session.add(category)
    await session.flush()
    event = await make_event(
        club=club, created_by=owner, name="DevFest", additional_details=[dict(x) for x in FORM],
        event_datetime=datetime.now(timezone.utc) + timedelta(days=9), category_id=category.id,
    )
    app_user = await make_user(email="app@example.com")
    mailer = await make_user(email="mailer@example.com")
    done_user = await make_user(email="done@example.com")
    req_user = await make_user(email="req@example.com")
    session.add(UserDeviceTokens(user_id=app_user.id, fcm_token="tok-app", platform="android"))
    app_reg = await make_registration(event=event, user=app_user, additional_details={"status": "Working"},
                                      phone="9876543210", whatsapp_opt_in=True)
    mail_reg = await make_registration(event=event, user=mailer, additional_details={"status": "Student", "tshirt": "S"})
    done_reg = await make_registration(event=event, user=done_user, additional_details={"status": "Working", "tshirt": "M"})
    req = RegistrationRequests(
        id=uuid.uuid4(), event_id=event.id, user_id=req_user.id, full_name="Riya Request", email=req_user.email,
        phone="9876500000", answers={"status": "Working"}, status="pending", token=secrets.token_urlsafe(16),
    )
    session.add(req)
    await session.commit()
    return dict(
        owner=owner, club=club, event=event, app_user=app_user, mailer=mailer, done_user=done_user, req_user=req_user,
        app_reg=app_reg, mail_reg=mail_reg, done_reg=done_reg, req=req, h=auth_header(await mint_token(owner)),
        app_h=auth_header(await mint_token(app_user)), mailer_h=auth_header(await mint_token(mailer)),
        req_h=auth_header(await mint_token(req_user)),
    )


async def _ask(client, world, **body):
    payload = {"questions": ["tshirt"], "channels": ["push", "email"], "message": "We need your T-shirt size."}
    payload.update(body)
    return await client.post(f"{API}/events/{world['event'].id}/answers/asks", json=payload, headers=world["h"])


async def _sends(session, ask_id):
    return list((await session.scalars(select(AnswerAskSends).where(AnswerAskSends.ask_id == ask_id))).all())


def _link(world, ref):
    return people_mod.personal_link(world["event"].id, ref)


# ─── §2.1 preview ────────────────────────────────────────────────────────


async def test_the_preview_counts_people_and_each_channels_reach(client, world, monkeypatch):
    from app.api.club_whatsapp import service as accounts

    async def no_feature(session, club_id, actor=None):
        return False

    monkeypatch.setattr(accounts, "plan_allows", no_feature)
    r = await client.post(f"{API}/events/{world['event'].id}/answers/asks/preview", json={"questions": ["tshirt"]}, headers=world["h"])
    assert r.status_code == 200, r.text
    assert r.json() == {
        "people": 2, "reach": {"push": 1, "email": 2, "whatsapp": 0}, "whatsapp": {"available": False, "reason": "NO_FEATURE"},
    }
    without = await client.post(
        f"{API}/events/{world['event'].id}/answers/asks/preview", json={"questions": ["tshirt"], "include_requests": False}, headers=world["h"],
    )
    assert without.json()["people"] == 1
    one = await client.post(
        f"{API}/events/{world['event'].id}/answers/asks/preview",
        json={"questions": ["tshirt", "college"], "refs": [str(world["mail_reg"].id)]}, headers=world["h"],
    )
    assert one.json()["people"] == 1


async def test_whatsapp_says_why_it_is_not_on_offer(session, world, monkeypatch):
    from types import SimpleNamespace

    from app.api.club_whatsapp import crypto, templates
    from app.api.club_whatsapp import service as accounts

    async def yes(session, club_id, actor=None):
        return True

    async def none_(session, club_id):
        return None

    monkeypatch.setattr(accounts, "plan_allows", yes)
    monkeypatch.setattr(accounts, "live_account", none_)
    assert await asks_mod.whatsapp_route(session, world["club"].id) == (None, "NOT_CONNECTED")

    account = SimpleNamespace(id=1, status="active", restrictions=None)

    async def live(session, club_id):
        return account

    async def no_language(session, account_id, name):
        assert name == "details_needed"
        return None

    monkeypatch.setattr(accounts, "live_account", live)
    monkeypatch.setattr(accounts, "sending_restricted", lambda a, now=None: False)
    monkeypatch.setattr(crypto, "available", lambda: True)
    monkeypatch.setattr(templates, "usable_language", no_language)
    assert await asks_mod.whatsapp_route(session, world["club"].id) == (None, "TEMPLATE_NOT_APPROVED")


# ─── §2.1 asking ─────────────────────────────────────────────────────────


async def test_an_ask_goes_to_everyone_askable_on_each_channel_that_reaches_them(client, session, world, sent):
    r = await _ask(client, world, deadline=(datetime.now(timezone.utc) + timedelta(days=3)).isoformat(), client_ref="c-1")
    assert r.status_code == 201, r.text
    ask = r.json()
    assert ask["status"] == "open" and ask["questions"] == [{"key": "tshirt", "label": "T-shirt size"}]
    assert ask["channels"] == ["push", "email"] and ask["message"] == "We need your T-shirt size."
    assert ask["created_by"] == "Test User" and ask["reminders_sent"] == 0 and ask["next_reminder_at"] is None
    # Made with the messages queued: one job per person sends them after the commit.
    assert ask["counts"] == {"people": 2, "sent": 0, "failed": 0, "opened": 0, "answered": 0, "still_askable": 2}
    [now] = (await client.get(f"{API}/events/{world['event'].id}/answers/asks", headers=world["h"])).json()["asks"]
    assert now["counts"] == {"people": 2, "sent": 2, "failed": 0, "opened": 0, "answered": 0, "still_askable": 2}
    # The app user: a push with their link, and an email. The request: an email (no app).
    [push] = sent.push
    assert push["user_id"] == world["app_user"].id and push["title"] == "DevFest needs 1 more detail"
    assert push["body"] == "We need your T-shirt size."
    assert push["data"] == {"type": "answers_needed", "event_id": str(world["event"].id), "url": _link(world, str(world["app_reg"].id))}
    assert sorted(m["recipients"][0] for m in sent.mail) == ["app@example.com", "req@example.com"]
    mail = next(m for m in sent.mail if m["recipients"] == ["req@example.com"])
    assert mail["subject"] == "DevFest needs 1 more detail from you"
    assert "T-shirt size" in mail["text"] and _link(world, f"q{world['req'].id}") in mail["text"] and "Add my details" in mail["html"]
    assert mail["sender_name"] == "Ask Club"
    rows = {(s.ref_id, s.channel): s for s in await _sends(session, uuid.UUID(ask["id"]))}
    assert rows[(world["app_reg"].id, "push")].status == "sent"
    assert rows[(world["req"].id, "push")].status == "skipped" and "app" in rows[(world["req"].id, "push")].error
    assert rows[(world["req"].id, "email")].status == "sent" and rows[(world["req"].id, "email")].provider_id
    # The first round is free of the club's quota.
    assert sent.counted == []
    # A double click is one ask.
    again = await _ask(client, world, client_ref="c-1")
    assert again.status_code == 201 and again.json()["id"] == ask["id"]
    listed = (await client.get(f"{API}/events/{world['event'].id}/answers/asks", headers=world["h"])).json()["asks"]
    assert [a["id"] for a in listed] == [ask["id"]]
    summary = (await client.get(f"{API}/events/{world['event'].id}/answers/summary", headers=world["h"])).json()
    assert summary["open_asks"] == 1


async def test_ask_again_for_one_person(client, world, sent):
    r = await _ask(client, world, questions=["college", "tshirt"], refs=[str(world["mail_reg"].id)], channels=["email"])
    assert r.status_code == 201, r.text
    assert r.json()["counts"]["people"] == 1
    assert [m["recipients"] for m in sent.mail] == [["mailer@example.com"]]
    assert "• College" in sent.mail[0]["text"] and "• T-shirt size" not in sent.mail[0]["text"]


async def test_nobody_to_ask_and_an_event_over_are_409(client, session, world):
    r = await _ask(client, world, refs=[str(world["done_reg"].id)])
    assert r.status_code == 409 and r.json()["error_code"] == "NOTHING_TO_ASK"
    event = await session.get(Events, world["event"].id)
    event.event_datetime = datetime.now(timezone.utc) - timedelta(days=2)
    await session.commit()
    r = await _ask(client, world)
    assert r.status_code == 409 and r.json()["error_code"] == "EVENT_ENDED"


@pytest.mark.parametrize("body, field", [
    ({"deadline": "2020-01-01T00:00:00+05:30"}, "deadline"),
    ({"questions": ["nope"]}, "questions"),
    ({"message": "   "}, "message"),
])
async def test_an_ask_is_checked(client, world, body, field):
    r = await _ask(client, world, **body)
    assert r.status_code == 422, r.text
    assert field in (r.json().get("errors") or {}), r.text


async def test_a_deadline_after_the_event_and_odd_reminders_are_refused(client, world):
    late = (world["event"].event_datetime + timedelta(days=1)).isoformat()
    assert (await _ask(client, world, deadline=late)).status_code == 422
    assert (await _ask(client, world, remind_after_hours=5)).status_code == 422
    assert (await _ask(client, world, channels=[])).status_code == 422


async def test_whatsapp_cannot_be_chosen_when_the_club_cannot_send_it(client, world):
    r = await _ask(client, world, channels=["whatsapp"])
    assert r.status_code == 422 and r.json()["error_code"] == "WHATSAPP_UNAVAILABLE"


# ─── Reminders and closing ───────────────────────────────────────────────


async def _age(session, ask_id, hours):
    ask = await session.get(AnswerAsks, uuid.UUID(str(ask_id)))
    ask.last_round_at = datetime.now(timezone.utc) - timedelta(hours=hours)
    await session.commit()


async def test_reminders_twelve_hours_apart_at_most_twice_and_counted(client, session, world, sent):
    ask = (await _ask(client, world, channels=["email"])).json()
    base = f"{API}/events/{world['event'].id}/answers/asks/{ask['id']}"
    soon = await client.post(f"{base}/remind", headers=world["h"])
    assert soon.status_code == 409 and soon.json()["error_code"] == "REMIND_TOO_SOON"
    assert 0 < soon.json()["errors"]["retry_after"] <= 12 * 3600
    await _age(session, ask["id"], 13)
    first = await client.post(f"{base}/remind", headers=world["h"])
    assert first.status_code == 200, first.text
    assert first.json()["reminders_sent"] == 1
    reminders = [m for m in sent.mail if m["subject"].startswith("Reminder")]
    assert sorted(m["recipients"][0] for m in reminders) == ["app@example.com", "req@example.com"]
    assert reminders[0]["subject"] == "Reminder: 1 detail needed for DevFest"
    # A reminder counts like any message.
    assert [c["kind"] for c in sent.counted] == ["reminder", "reminder"] and all(c["channel"] == "email" for c in sent.counted)
    rounds = {s.round for s in await _sends(session, uuid.UUID(ask["id"]))}
    assert rounds == {0, 1}
    await _age(session, ask["id"], 13)
    assert (await client.post(f"{base}/remind", headers=world["h"])).json()["reminders_sent"] == 2
    await _age(session, ask["id"], 13)
    limit = await client.post(f"{base}/remind", headers=world["h"])
    assert limit.status_code == 409 and limit.json()["error_code"] == "REMIND_LIMIT"


async def test_a_reminder_goes_only_to_people_still_askable(client, session, world, sent):
    ask = (await _ask(client, world, channels=["email"])).json()
    world["app_reg"].additional_details = {"status": "Working", "tshirt": "L"}
    await session.commit()
    await _age(session, ask["id"], 13)
    sent.mail.clear()
    r = await client.post(f"{API}/events/{world['event'].id}/answers/asks/{ask['id']}/remind", headers=world["h"])
    assert r.status_code == 200
    assert [m["recipients"] for m in sent.mail] == [["req@example.com"]]


async def test_closing_an_ask(client, world):
    ask = (await _ask(client, world)).json()
    base = f"{API}/events/{world['event'].id}/answers/asks/{ask['id']}"
    closed = await client.post(f"{base}/close", headers=world["h"])
    assert closed.status_code == 200 and closed.json()["status"] == "closed" and closed.json()["closed_reason"] == "closed_by_club"
    r = await client.post(f"{base}/remind", headers=world["h"])
    assert r.status_code == 409 and r.json()["error_code"] == "CLOSED"


async def test_an_ask_closes_by_itself_at_its_deadline(client, session, world):
    ask = (await _ask(client, world, deadline=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat())).json()
    row = await session.get(AnswerAsks, uuid.UUID(ask["id"]))
    row.deadline = datetime.now(timezone.utc) - timedelta(minutes=1)
    await session.commit()
    [listed] = (await client.get(f"{API}/events/{world['event'].id}/answers/asks", headers=world["h"])).json()["asks"]
    assert listed["status"] == "closed" and listed["closed_reason"] == "deadline"


async def test_the_automatic_reminder(client, session, world, sent):
    ask = (await _ask(client, world, channels=["email"], remind_after_hours=24)).json()
    assert ask["next_reminder_at"] is not None
    job = await session.scalar(select(jobs.Jobs).where(jobs.Jobs.name == asks_mod.REMIND_JOB))
    assert job is not None and job.run_at > datetime.now(timezone.utc) + timedelta(hours=23)
    row = await session.get(AnswerAsks, uuid.UUID(ask["id"]))
    row.next_reminder_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    row.last_round_at = datetime.now(timezone.utc) - timedelta(hours=25)
    await session.commit()
    sent.mail.clear()
    await asks_mod._remind_job({"ask_id": ask["id"]})
    await jobs.settle()
    await session.refresh(row)
    assert row.reminders_sent == 1 and row.next_reminder_at is None
    assert len(sent.mail) == 2 and all(m["subject"].startswith("Reminder") for m in sent.mail)


# ─── §2.2, §2.3 the personal link ────────────────────────────────────────


def _path(world, ref, k=None):
    return f"{API}/events/{world['event'].id}/answers/{ref}" + (f"?k={k}" if k is not None else "")


def _k(world, ref):
    return people_mod.signature(world["event"].id, ref)


async def test_the_link_shows_whats_asked_and_marks_it_opened(client, session, world):
    ask = (await _ask(client, world, deadline=(datetime.now(timezone.utc) + timedelta(days=2)).isoformat())).json()
    ref = f"q{world['req'].id}"
    r = await client.get(_path(world, ref, _k(world, ref)))
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "open" and out["asked"] == ["tshirt"] and out["answers"] == {"status": "Working"}
    assert out["person"] == {"first_name": "Riya", "kind": "request"}
    assert out["event"]["name"] == "DevFest" and out["event"]["org_name"] == "Ask Club" and out["event"]["id"] == world["event"].id
    assert out["message"] == "We need your T-shirt size." and out["deadline"] is not None
    assert [x["key"] for x in out["questions"]] == ["status", "college", "tshirt", "why"]
    college = out["questions"][1]
    assert college["required"] is False and college["required_when_shown"] is True  # the wire shape
    assert out["ticket_url"] is None and out["request_url"].endswith(f"/requests/{world['req'].token}")
    assert r.headers["cache-control"] == "no-store"
    opened = await session.scalar(select(AnswerAskPeople).where(AnswerAskPeople.ref_id == world["req"].id))
    assert opened.opened_at is not None
    [listed] = (await client.get(f"{API}/events/{world['event'].id}/answers/asks", headers=world["h"])).json()["asks"]
    assert listed["id"] == ask["id"] and listed["counts"]["opened"] == 1


@pytest.mark.parametrize("case", ["bad_k", "no_k", "bad_ref", "other_event_ref"])
async def test_a_bad_link_is_a_404(client, session, world, make_event, case):
    ref = str(world["app_reg"].id)
    path = {
        "bad_k": _path(world, ref, "0" * 32),
        "no_k": _path(world, ref),
        "bad_ref": _path(world, "not-a-ref", "x"),
        "other_event_ref": f"{API}/events/{world['event'].id}/answers/{uuid.uuid4()}?k=x",
    }[case]
    assert (await client.get(path)).status_code == 404


async def test_the_holders_own_sign_in_opens_it_without_k(client, world):
    ref = str(world["app_reg"].id)
    assert (await client.get(_path(world, ref), headers=world["app_h"])).status_code == 200
    assert (await client.get(_path(world, ref), headers=world["mailer_h"])).status_code == 404
    # A club's own sign-in in the same browser is no refusal of the link, and no key either.
    assert (await client.get(_path(world, ref, _k(world, ref)), headers=world["h"])).status_code == 200
    assert (await client.get(_path(world, ref), headers=world["h"])).status_code == 404
    req_ref = f"q{world['req'].id}"
    assert (await client.get(_path(world, req_ref), headers=world["req_h"])).status_code == 200


async def test_answering_checks_merges_and_marks_the_ask_answered(client, session, world):
    ask = (await _ask(client, world)).json()
    ref = str(world["app_reg"].id)
    bad = await client.post(_path(world, ref, _k(world, ref)), json={"answers": {"tshirt": "XXL"}})
    assert bad.status_code == 422 and bad.json()["errors"] == {"tshirt": "Invalid option selected"}
    ok = await client.post(_path(world, ref, _k(world, ref)), json={"answers": {"tshirt": "L", "why": "not asked", "status": "Student"}})
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "done" and ok.json()["asked"] == []
    await session.refresh(world["app_reg"])
    # Only what was asked was taken: status and why were not.
    assert world["app_reg"].additional_details == {"status": "Working", "tshirt": "L"}
    row = await session.scalar(select(AnswerAskPeople).where(AnswerAskPeople.ref_id == world["app_reg"].id))
    assert row.answered_at is not None
    [listed] = (await client.get(f"{API}/events/{world['event'].id}/answers/asks", headers=world["h"])).json()["asks"]
    assert listed["id"] == ask["id"] and listed["counts"]["answered"] == 1 and listed["counts"]["still_askable"] == 1


async def test_answering_a_parent_reveals_its_children_which_are_then_required(client, session, world, make_user, make_registration):
    blank = await make_registration(event=world["event"], user=await make_user(email="blank@example.com"), additional_details={})
    await session.commit()
    assert (await _ask(client, world, questions=["status"], channels=["email"])).status_code == 201
    ref = str(blank.id)
    short = await client.post(_path(world, ref, _k(world, ref)), json={"answers": {"status": "Student"}})
    assert short.status_code == 422 and short.json()["errors"] == {"college": "This field is required", "tshirt": "This field is required"}
    full = await client.post(_path(world, ref, _k(world, ref)), json={"answers": {"status": "Student", "college": "IIT", "tshirt": "S"}})
    assert full.status_code == 200, full.text
    await session.refresh(blank)
    assert blank.additional_details == {"status": "Student", "college": "IIT", "tshirt": "S"}
    # Switching to Working drops the student answer (hidden ones pruned).
    world_ask = await _ask(client, world, questions=["why"], refs=[ref], channels=["email"])
    assert world_ask.status_code == 201


async def test_everyone_answered_closes_the_ask(client, session, world):
    ask = (await _ask(client, world)).json()
    for ref in (str(world["app_reg"].id), f"q{world['req'].id}"):
        r = await client.post(_path(world, ref, _k(world, ref)), json={"answers": {"tshirt": "M"}})
        assert r.status_code == 200, r.text
    row = await session.get(AnswerAsks, uuid.UUID(ask["id"]))
    await session.refresh(row)
    assert row.status == "closed" and row.closed_reason == "done"
    await session.refresh(world["req"])
    assert world["req"].answers == {"status": "Working", "tshirt": "M"}


async def test_after_the_deadline_the_link_is_closed(client, session, world):
    ask = (await _ask(client, world, deadline=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat())).json()
    row = await session.get(AnswerAsks, uuid.UUID(ask["id"]))
    row.deadline = datetime.now(timezone.utc) - timedelta(minutes=1)
    await session.commit()
    ref = str(world["app_reg"].id)
    assert (await client.get(_path(world, ref, _k(world, ref)))).json()["status"] == "closed"
    r = await client.post(_path(world, ref, _k(world, ref)), json={"answers": {"tshirt": "M"}})
    assert r.status_code == 409 and r.json()["error_code"] == "CLOSED"


async def test_someone_no_longer_registered_has_nothing_to_answer(client, session, world):
    await _ask(client, world)
    world["app_reg"].is_deleted = True
    await session.commit()
    ref = str(world["app_reg"].id)
    out = (await client.get(_path(world, ref, _k(world, ref)))).json()
    assert out["status"] == "nothing" and out["questions"] == [] and out["answers"] == {} and out["asked"] == []


async def test_a_missing_answer_with_no_ask_can_still_be_given(client, world):
    ref = str(world["mail_reg"].id)
    out = (await client.get(_path(world, ref, _k(world, ref)))).json()
    assert out["status"] == "open" and out["asked"] == ["college"] and out["message"] is None
    done = (await client.get(_path(world, str(world["done_reg"].id), _k(world, str(world["done_reg"].id))))).json()
    assert done["status"] == "done"


# ─── §2.4 the flags ──────────────────────────────────────────────────────


async def test_answers_needed_on_the_ticket_lists_and_request_only_while_asked(client, session, world):
    ticket = f"{API}/events/tickets/{world['app_reg'].ticket_id}"
    assert (await client.get(ticket, headers=world["app_h"])).json()["answers_needed"] is None  # missing, but not asked
    deadline = datetime.now(timezone.utc) + timedelta(days=2)
    await _ask(client, world, deadline=deadline.isoformat())
    flag = (await client.get(ticket, headers=world["app_h"])).json()["answers_needed"]
    assert flag["count"] == 1 and flag["url"] == _link(world, str(world["app_reg"].id)) and flag["deadline"] is not None
    # The club reading the ticket is not its holder.
    assert (await client.get(ticket, headers=world["h"])).json()["answers_needed"] is None
    mine = (await client.get(f"{API}/user/my-events", headers=world["app_h"])).json()["items"]
    assert mine[0]["answers_needed"]["count"] == 1
    listed = (await client.get(f"{API}/events/list", params={"is_registered": "true"}, headers=world["app_h"])).json()["items"]
    assert next(i for i in listed if i["id"] == world["event"].id)["answers_needed"]["count"] == 1
    view = (await client.get(f"{API}/registration-requests/{world['req'].token}")).json()
    assert view["answers_needed"]["url"] == _link(world, f"q{world['req'].id}")
    [row] = (await client.get(f"{API}/registration-requests/mine", headers=world["req_h"])).json()["items"]
    assert row["answers_needed"] == {**view["answers_needed"]}
    # Answered: the flag goes.
    ref = str(world["app_reg"].id)
    await client.post(_path(world, ref, _k(world, ref)), json={"answers": {"tshirt": "S"}})
    assert (await client.get(ticket, headers=world["app_h"])).json()["answers_needed"] is None
    req_ref = f"q{world['req'].id}"
    await client.post(_path(world, req_ref, _k(world, req_ref)), json={"answers": {"tshirt": "S"}})
    [row] = (await client.get(f"{API}/registration-requests/mine", headers=world["req_h"])).json()["items"]
    assert row["answers_needed"] is None


# ─── §2.5 the door ───────────────────────────────────────────────────────


async def test_the_door_sees_missing_answers_and_fills_them(client, session, world):
    ask = (await _ask(client, world, refs=[str(world["app_reg"].id)])).json()
    scan = await client.post(
        f"{API}/events/{world['event'].id}/gate/scan", json={"code": world["app_reg"].ticket_id, "client_ref": "scan-ma-0001"}, headers=world["h"],
    )
    assert scan.status_code == 200, scan.text
    assert scan.json()["outcome"] == "CHECKED_IN"  # never blocked
    assert scan.json()["attendee"]["missing_answers"] == [{"key": "tshirt", "label": "T-shirt size"}]
    found = (await client.get(f"{API}/events/{world['event'].id}/gate/search", params={"q": "app@"}, headers=world["h"])).json()
    assert found[0]["missing_answers"] == [{"key": "tshirt", "label": "T-shirt size"}]
    path = f"{API}/events/{world['event'].id}/gate/answers/{world['app_reg'].id}"
    wrong = await client.post(path, json={"answers": {"tshirt": "XXL"}}, headers=world["h"])
    assert wrong.status_code == 422 and wrong.json()["errors"] == {"tshirt": "Invalid option selected"}
    # Relaxed: nothing required, blanks leave things as they are.
    ok = await client.post(path, json={"answers": {"tshirt": "M", "why": ""}}, headers=world["h"])
    assert ok.status_code == 200, ok.text
    assert ok.json() == {"missing_answers": []}
    await session.refresh(world["app_reg"])
    assert world["app_reg"].additional_details == {"status": "Working", "tshirt": "M"}
    row = await session.get(AnswerAsks, uuid.UUID(ask["id"]))
    await session.refresh(row)
    assert row.status == "closed" and row.closed_reason == "done"


async def test_a_club_correction_counts_as_answered(client, session, world):
    await _ask(client, world, refs=[str(world["app_reg"].id)])
    r = await client.patch(
        f"{API}/events/registration/{world['event'].id}/info/{world['app_reg'].id}",
        json={"additional_details": {"tshirt": "S"}}, headers=world["h"],
    )
    assert r.status_code == 200 and r.json()["missing_answers"] == []
    row = await session.scalar(select(AnswerAskPeople).where(AnswerAskPeople.ref_id == world["app_reg"].id))
    assert row.answered_at is not None


# ─── §2.6 the club's own WhatsApp ────────────────────────────────────────


async def test_whatsapp_goes_from_the_clubs_number_with_its_template(client, session, world, sent, monkeypatch):
    from app.core import whatsapp

    sender = whatsapp.Sender(phone_number_id="123456", token="t" * 30, language="en")

    async def route(session, club_id):
        return sender, None

    monkeypatch.setattr(asks_mod, "whatsapp_route", route)
    ask = (await _ask(client, world, channels=["whatsapp"])).json()
    [listed] = (await client.get(f"{API}/events/{world['event'].id}/answers/asks", headers=world["h"])).json()["asks"]
    assert listed["counts"]["sent"] == 1
    [wa] = sent.whatsapp
    assert wa["number"] == "9876543210" and wa["sender"] is sender
    template = wa["template"]
    assert template.name == "details_needed"
    # make_registration names everyone "Test User"; no deadline: "the event".
    assert template.body == ("Test", "DevFest", "1", "the event")
    ref = str(world["app_reg"].id)
    assert template.button == f"{world['event'].id}/{ref}?k={_k(world, ref)}"
    rows = {s.ref_id: s for s in await _sends(session, uuid.UUID(ask["id"]))}
    assert rows[world["app_reg"].id].status == "sent" and rows[world["app_reg"].id].provider_id == "wamid.1"
    assert rows[world["req"].id].status == "skipped"  # never ticked WhatsApp
    # The club's own number is never counted.
    assert sent.counted == []


async def test_details_needed_is_a_standard_template_of_the_shape_sends_fill():
    from app.api.club_whatsapp import templates
    from app.core import whatsapp
    from app.core.notifications import delivery

    spec = templates.STANDARD["details_needed"]
    assert spec["body"] == "Hi {{1}}, {{2}} needs {{3}} more details for your registration. Please add them by {{4}}."
    body = templates.standard_body("details_needed", None)
    assert body["category"] == "UTILITY"
    button = body["components"][-1]["buttons"][0]
    assert button["text"] == "Add my details" and button["url"] == "https://myotherapp.in/answers/{{1}}"
    assert templates.fits("details_needed", body["components"], "UTILITY")
    filled = whatsapp.details_needed(name="Haritha K", event="DevFest", count=2, by="Sat 14 Nov, 6:00 PM", suffix="7/q1a?k=ab")
    assert filled.body == ("Haritha", "DevFest", "2", "Sat 14 Nov, 6:00 PM") and filled.button == "7/q1a?k=ab"
    assert "answers_needed" in delivery.PERSONAL
