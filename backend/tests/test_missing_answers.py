"""Missing answers, phase 1 (feature-register/missing-answers-contract.md §1):
keys that stay put, form versions, the impact preview, the summary, the
lists' marker and filter, the exports by label, and moving stray answers.

The form every test here starts from:

    status   radio   [Student, Working]  required
    college  text                        required  show_if status in [Student]
    tshirt   select  [S, M, L]           required
    why      textarea                    optional
"""
import csv
import io
import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from openpyxl import load_workbook
from sqlalchemy import select

from app.api.approvals.models import RegistrationRequests
from app.api.club_access import policy
from app.api.events import questions
from app.api.events.models import EventCategories, EventRegistrationsLink, Events
from app.api.missing_answers import service as ma
from app.api.missing_answers.models import EventFormRevisions
from app.api.missing_answers.state import Form
from app.core.rate_limit import limiter
from tests.conftest import auth_header

#: Every test opens the suite's session, those that never touch the database
#: too: a first test without it leaves the session's connection on another loop.
pytestmark = pytest.mark.usefixtures("session")

API = "/api/v1"
STUDENT = {"key": "status", "any_of": ["Student"]}


def q(key, label, kind, options=None, show_if=None, required=True):
    out = {"key": key, "label": label, "field_type": kind, "required": required, "options": options}
    if show_if is not None:
        out["show_if"] = show_if
    return out


def base_form() -> list[dict]:
    return [
        q("status", "Student or working?", "radio", ["Student", "Working"]),
        q("college", "College", "text", show_if=STUDENT),
        q("tshirt", "T-shirt size", "select", ["S", "M", "L"]),
        q("why", "Why are you coming?", "textarea", required=False),
    ]


@pytest.fixture(autouse=True)
def _no_ticket_mail(monkeypatch):
    from app.core.email import email as email_mod

    monkeypatch.setattr(email_mod, "send_email_with_attachment", lambda **kwargs: None)
    monkeypatch.setattr(limiter, "enabled", False)


async def _request(session, event, user, answers, *, status="pending", email=None, registration_id=None):
    req = RegistrationRequests(
        id=uuid.uuid4(), event_id=event.id, user_id=user.id if user else None, full_name="Request Person",
        email=email or (user.email if user else f"req-{uuid.uuid4().hex[:6]}@example.com"), phone="9876543210",
        answers=answers, status=status, token=secrets.token_urlsafe(16), registration_id=registration_id,
    )
    session.add(req)
    await session.flush()
    return req


@pytest.fixture
async def world(session, make_club, make_event, make_user, make_registration, mint_token):
    owner, club = await make_club(name="Answers Club", email="answers-club@example.com")
    category = EventCategories(name="Talks")
    session.add(category)
    await session.flush()
    event = await make_event(
        club=club, created_by=owner, name="DevFest", additional_details=base_form(),
        event_datetime=datetime.now(timezone.utc) + timedelta(days=9), category_id=category.id,
    )
    users = [await make_user(email=f"person{i}@example.com") for i in range(6)]
    # Everything answered.
    full = await make_registration(event=event, user=users[0], additional_details={"status": "Working", "tshirt": "M", "why": "Learn"})
    # Student, college blank: missing college.
    no_college = await make_registration(event=event, user=users[1], additional_details={"status": "Student", "tshirt": "S"})
    # An answer no longer among the options: tshirt invalid; and an answer under an old key.
    invalid = await make_registration(event=event, user=users[2], additional_details={"status": "Working", "tshirt": "XXL", "collage": "NIT"})
    # Cancelled and revoked registrations never count.
    await make_registration(event=event, user=users[3], additional_details={}, is_deleted=True)
    await make_registration(event=event, user=users[4], additional_details={}, ticket_revoked_at=datetime.now(timezone.utc))
    # An open request with nothing answered, and a declined one that never counts.
    pending = await _request(session, event, users[5], {"status": "Working"})
    await _request(session, event, None, {}, status="declined")
    await session.commit()
    return dict(
        owner=owner, club=club, category=category, event=event, users=users, full=full, no_college=no_college,
        invalid=invalid, pending=pending, h=auth_header(await mint_token(owner)),
    )


# ─── §1.1 keys that stay put ─────────────────────────────────────────────


async def test_a_reworded_label_keeps_the_stored_key_and_its_children_follow():
    before = base_form()
    sent = [dict(x) for x in before]
    # The editor re-derived the parent's key from the same label, re-spaced and re-cased.
    sent[0] = {**sent[0], "key": "student_or_working", "label": "  student OR   working? "}
    sent[1] = {**sent[1], "show_if": {"key": "student_or_working", "any_of": ["Student"]}}
    kept = questions.keep_keys(sent, before)
    assert kept[0]["key"] == "status" and kept[0]["label"] == "  student OR   working? "
    assert kept[1]["show_if"] == {"key": "status", "any_of": ["Student"]}


async def test_a_new_label_or_an_ambiguous_one_is_left_as_sent():
    before = base_form()
    other = [dict(x) for x in before]
    other[2] = {**other[2], "key": "shirt", "label": "Shirt size"}
    assert questions.keep_keys(other, before)[2]["key"] == "shirt"
    # Two new questions with the label of one gone: not certain, so neither takes it.
    two = [dict(x) for x in before if x["key"] != "tshirt"] + [
        q("tshirt_1", "T-shirt size", "select", ["S"]), q("tshirt_2", "t-shirt size", "select", ["M"]),
    ]
    assert [x["key"] for x in questions.keep_keys(two, before)][-2:] == ["tshirt_1", "tshirt_2"]


async def _update(client, world, details):
    form = {
        "name": world["event"].name, "event_datetime": world["event"].event_datetime.isoformat(), "duration": "2",
        "has_fee": "false", "category_id": str(world["category"].id), "additional_details": json.dumps(details),
    }
    return await client.put(f"{API}/events/update/{world['event'].id}", data=form, headers=world["h"])


async def test_update_keeps_the_stored_key_and_never_touches_answers(client, session, world):
    sent = questions.wire_all(base_form())
    sent[2] = {**sent[2], "key": "t_shirt_size", "label": "T-Shirt  Size"}
    r = await _update(client, world, sent)
    assert r.status_code == 200, r.text
    stored = (await session.get(Events, world["event"].id)).additional_details
    assert [x["key"] for x in stored] == ["status", "college", "tshirt", "why"]
    assert stored[2]["label"] == "T-Shirt  Size"
    await session.refresh(world["full"])
    assert world["full"].additional_details == {"status": "Working", "tshirt": "M", "why": "Learn"}


async def test_update_now_refuses_a_choice_question_without_options(client, world):
    sent = questions.wire_all(base_form())
    sent[2] = {**sent[2], "options": None}
    r = await _update(client, world, sent)
    assert r.status_code == 400, r.text
    assert "Options required" in r.json()["message"]


# ─── §1.2 form versions ──────────────────────────────────────────────────


async def _versions(session, event_id):
    return list((await session.scalars(
        select(EventFormRevisions).where(EventFormRevisions.event_id == event_id).order_by(EventFormRevisions.version)
    )).all())


async def test_the_first_change_writes_the_old_form_and_the_new_one(client, session, world):
    r = await _update(client, world, questions.wire_all(base_form()))
    assert r.status_code == 200 and r.json()["form_version"] is None  # nothing changed, nothing written
    assert await _versions(session, world["event"].id) == []
    new = base_form() + [q("diet", "Diet", "select", ["Veg", "Non-veg"])]
    r = await _update(client, world, questions.wire_all(new))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["form_version"] == 2 and body["form_changed_at"]
    v1, v2 = await _versions(session, world["event"].id)
    assert v1.version == 1 and v1.questions == base_form() and v1.summary is None
    assert v2.version == 2 and [x["key"] for x in v2.questions][-1] == "diet"
    assert v2.summary["added"] == [{"key": "diet", "label": "Diet", "required": True, "askable": 4}]
    assert v2.changed_by_user_id == world["owner"].id
    # A second change: one more version, numbered after.
    r = await _update(client, world, questions.wire_all(new[:-1]))
    assert r.json()["form_version"] == 3
    assert [v.version for v in await _versions(session, world["event"].id)] == [1, 2, 3]


async def test_the_dashboards_payloads_carry_the_form_version(client, session, world):
    await _update(client, world, questions.wire_all(base_form()[:3]))
    info = (await client.get(f"{API}/events/info/{world['event'].id}", headers=world["h"])).json()
    assert info["form_version"] == 2 and info["form_changed_at"]
    public = (await client.get(f"{API}/events/info/{world['event'].id}")).json()
    assert public["form_version"] is None and public["form_changed_at"] is None
    listed = (await client.get(f"{API}/clubs/events/list", headers=world["h"])).json()
    row = next(i for i in listed["items"] if i["id"] == world["event"].id)
    assert row["form_version"] == 2 and row["form_changed_at"]


# ─── §1.3 impact ─────────────────────────────────────────────────────────


async def test_the_impact_of_a_save_before_it_is_made(client, session, world):
    after = [
        q("status", "Student or working?", "radio", ["Student", "Working"]),
        q("college", "College name", "text", show_if=STUDENT),
        q("tshirt", "T-shirt size", "select", ["S", "M"]),           # L dropped
        q("why", "Why are you coming?", "number", required=True),     # now required, and a number
        q("diet", "Diet", "select", ["Veg", "Non-veg"]),
    ]
    r = await client.post(f"{API}/events/{world['event'].id}/form/impact", json={"additional_details": questions.wire_all(after)}, headers=world["h"])
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["people"] == {"registrations": 3, "requests": 1}
    assert out["added"] == [{"key": "diet", "label": "Diet", "required": True, "askable": 4}]
    assert out["removed"] == []
    assert out["now_required"] == [{"key": "why", "label": "Why are you coming?", "missing": 4}]
    assert out["options_removed"] == [{"key": "tshirt", "label": "T-shirt size", "option": "L", "chosen_by": 0}]
    assert out["relabelled"] == [{"key": "college", "from": "College", "to": "College name"}]
    assert out["type_changed"] == [{"key": "why", "label": "Why are you coming?", "from": "textarea", "to": "number", "invalid_after": 1}]
    # Nothing saved.
    assert (await session.get(Events, world["event"].id)).additional_details == base_form()
    removed = await client.post(
        f"{API}/events/{world['event'].id}/form/impact", json={"additional_details": questions.wire_all(base_form()[:3])}, headers=world["h"],
    )
    assert removed.json()["removed"] == [{"key": "why", "label": "Why are you coming?", "answered": 1}]


async def test_an_invalid_form_is_a_422_with_the_rules_words(client, world):
    bad = [q("college", "College", "text", show_if=STUDENT), q("status", "Student or working?", "radio", ["Student", "Working"])]
    r = await client.post(f"{API}/events/{world['event'].id}/form/impact", json={"additional_details": bad}, headers=world["h"])
    assert r.status_code == 422, r.text
    assert r.json()["error_code"] == "INVALID_FORM" and "'College'" in r.json()["message"]


async def test_a_question_that_isnt_one_is_a_422_not_a_crash(client, world):
    r = await client.post(f"{API}/events/{world['event'].id}/form/impact", json={"additional_details": [{"key": "x"}]}, headers=world["h"])
    assert r.status_code == 422 and r.json()["error_code"] == "INVALID_FORM" and "x" in r.json()["errors"]


async def test_loading_many_people_by_ref_falls_back_to_everyone(session, world):
    from app.api.missing_answers import people

    refs = [str(world["full"].id), f"q{world['pending'].id}"] + [str(uuid.uuid4()) for _ in range(people.MAX_IN)]
    found = await people.load(session, world["event"], refs=refs)
    assert {p.id for p in found} == {world["full"].id, world["pending"].id}


# ─── §1.4 the summary, the marker and the filter ─────────────────────────


async def test_each_question_is_hidden_answered_invalid_or_blank():
    form = Form(base_form())
    assert form.states({"status": "Student", "tshirt": "XXL"}) == {
        "status": "answered", "college": "blank", "tshirt": "invalid", "why": "blank",
    }
    assert form.states({"status": "Working", "tshirt": "M"})["college"] == "hidden"
    assert form.missing({"status": "Student", "tshirt": "XXL"}) == ["college", "tshirt"]
    assert form.askable({"status": "Student", "tshirt": "XXL"}) == ["college", "tshirt", "why"]
    assert form.stray({"collage": "NIT", "organization": "X", "empty": ""}) == ["collage"]


async def test_the_summary_counts_registrations_and_open_requests(client, world):
    r = await client.get(f"{API}/events/{world['event'].id}/answers/summary", headers=world["h"])
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["people"] == {"registrations": 3, "requests": 1}
    assert out["people_missing"] == 3  # no_college, invalid, the request
    by = {x["key"]: x for x in out["questions"]}
    assert by["college"] == {"key": "college", "label": "College", "required": True, "answered": 0, "blank": 1, "invalid": 0, "hidden": 3, "missing": 1}
    assert by["tshirt"]["invalid"] == 1 and by["tshirt"]["blank"] == 1 and by["tshirt"]["missing"] == 2
    assert by["why"]["missing"] == 0 and by["why"]["answered"] == 1
    assert out["stray"] == [{"key": "collage", "count": 1}]
    assert out["open_asks"] == 0


async def test_a_request_that_became_a_registration_counts_once(session, world, make_user, make_registration):
    user = await make_user(email="confirmed@example.com")
    reg = await make_registration(event=world["event"], user=user, additional_details={"status": "Working", "tshirt": "S"})
    await _request(session, world["event"], user, {"status": "Working"}, status="approved", registration_id=reg.id)
    await session.commit()
    out = await ma.summary(session, world["event"])
    assert out["people"] == {"registrations": 4, "requests": 1}


async def test_the_guest_list_marks_and_filters_who_is_missing_what(client, world):
    base = f"{API}/events/registration/{world['event'].id}/list"
    rows = {r["id"]: r for r in (await client.get(base, headers=world["h"])).json()["items"]}
    assert rows[str(world["full"].id)]["missing_answers"] == []
    assert rows[str(world["no_college"].id)]["missing_answers"] == ["college"]
    assert rows[str(world["invalid"].id)]["missing_answers"] == ["tshirt"]
    any_ = (await client.get(base, params={"missing": "1"}, headers=world["h"])).json()
    assert {r["id"] for r in any_["items"]} == {str(world["no_college"].id), str(world["invalid"].id)}
    assert any_["total"] == 2
    one = (await client.get(base, params={"missing": "college"}, headers=world["h"])).json()
    assert [r["id"] for r in one["items"]] == [str(world["no_college"].id)]
    assert (await client.get(base, params={"missing": "nope"}, headers=world["h"])).status_code == 422
    detail = (await client.get(f"{API}/events/registration/{world['event'].id}/info/{world['invalid'].id}", headers=world["h"])).json()
    assert detail["missing_answers"] == ["tshirt"]


async def test_the_review_list_marks_and_filters_requests(client, session, world):
    await _request(session, world["event"], None, {"status": "Working", "tshirt": "L"})
    await session.commit()
    base = f"{API}/events/registration/{world['event'].id}/requests"
    rows = (await client.get(base, headers=world["h"])).json()["items"]
    assert sorted(r["missing_answers"] for r in rows) == [[], ["tshirt"]]
    only = (await client.get(base, params={"missing": "1"}, headers=world["h"])).json()["items"]
    assert [r["id"] for r in only] == [str(world["pending"].id)]
    assert (await client.get(base, params={"missing": "why"}, headers=world["h"])).json()["items"] == []


async def test_the_export_is_by_label_with_a_missing_answers_column(client, session, world):
    event = await session.get(Events, world["event"].id)
    event.additional_details = base_form() + [q("tshirt2", "T-shirt size", "select", ["S", "M", "L"], required=False)]
    await session.commit()
    r = await client.get(f"{API}/events/registration/{world['event'].id}/export", headers=world["h"])
    assert r.status_code == 200, r.text
    rows = list(load_workbook(io.BytesIO(r.content)).active.iter_rows(values_only=True))
    header = list(rows[0])
    assert header[4:10] == ["Student or working?", "College", "T-shirt size (tshirt)", "Why are you coming?", "T-shirt size (tshirt2)", "collage"]
    assert header[-1] == "Missing answers"
    by_email = {row[2]: dict(zip(header, row)) for row in rows[1:]}
    assert by_email["person1@example.com"]["Missing answers"] == "College"
    assert by_email["person2@example.com"]["Missing answers"] == "T-shirt size"
    assert by_email["person0@example.com"]["Missing answers"] in (None, "")
    assert by_email["person2@example.com"]["collage"] == "NIT"


async def test_the_requests_csv_is_by_label_with_a_missing_answers_column(client, world):
    r = await client.get(f"{API}/events/registration/{world['event'].id}/requests/export", headers=world["h"])
    assert r.status_code == 200, r.text
    rows = list(csv.reader(io.StringIO(r.content.decode())))
    header = rows[0]
    assert ["Student or working?", "College", "T-shirt size", "Why are you coming?"] == header[7:11]
    assert header[-1] == "Missing answers"
    pending = next(row for row in rows[1:] if row[2] == "Request Person" and row[0] == "Under review")
    assert pending[-1] == "T-shirt size"


# ─── §1.5 moving stray answers ───────────────────────────────────────────


async def test_moving_answers_from_an_old_key(client, session, world, make_user, make_registration):
    both = await make_registration(
        event=world["event"], user=await make_user(email="both@example.com"),
        additional_details={"status": "Student", "college": "IIT", "collage": "NIT", "tshirt": "S"},
    )
    await session.commit()
    r = await client.post(f"{API}/events/{world['event'].id}/answers/move", json={"from": "collage", "to": "college"}, headers=world["h"])
    assert r.status_code == 200, r.text
    assert r.json() == {"moved": 1, "skipped": 1}
    await session.refresh(world["invalid"])
    await session.refresh(both)
    assert world["invalid"].additional_details == {"status": "Working", "tshirt": "XXL", "college": "NIT"}
    assert both.additional_details["collage"] == "NIT" and both.additional_details["college"] == "IIT"
    from app.api.audit.models import AdminAuditLogs

    logged = await session.scalar(select(AdminAuditLogs).where(AdminAuditLogs.action == "event.answers_moved"))
    assert logged is not None and logged.entity_id == str(world["event"].id)


@pytest.mark.parametrize("body", [{"from": "collage", "to": "nowhere"}, {"from": "status", "to": "college"}, {"from": "x", "to": "x"}])
async def test_moving_needs_an_old_key_and_a_current_one(client, world, body):
    r = await client.post(f"{API}/events/{world['event'].id}/answers/move", json=body, headers=world["h"])
    assert r.status_code == 422, r.text


@pytest.mark.parametrize("role, method, path, allowed", [
    ("admin", "POST", "/api/v1/events/7/answers/move", True),
    ("editor", "POST", "/api/v1/events/7/answers/move", False),
    ("door", "POST", "/api/v1/events/7/answers/move", False),
    ("editor", "POST", "/api/v1/events/7/answers/asks", True),
    ("editor", "POST", "/api/v1/events/7/answers/asks/5b0c1f2e-8a43-4f0e-9d3c-2b7f6a1e9c40/remind", True),
    ("editor", "POST", "/api/v1/events/7/form/impact", True),
    ("door", "POST", "/api/v1/events/7/answers/asks", False),
    ("door", "POST", "/api/v1/events/7/answers/asks/preview", False),
    ("door", "POST", "/api/v1/events/7/form/impact", False),
    ("door", "GET", "/api/v1/events/7/answers/summary", True),
    ("door", "GET", "/api/v1/events/7/answers/asks", True),
    ("door", "POST", "/api/v1/events/7/gate/answers/5b0c1f2e-8a43-4f0e-9d3c-2b7f6a1e9c40", True),
])
async def test_who_may_ask_move_and_fill(role, method, path, allowed):
    assert policy.allows(role, method, path) is allowed


async def test_the_dashboard_is_told_who_may_ask_and_move():
    assert policy.can("editor")["ask_answers"] is True and policy.can("editor")["move_answers"] is False
    assert policy.can("admin")["move_answers"] is True and policy.can("door")["ask_answers"] is False


async def test_a_registrations_answers_are_never_rewritten_by_a_form_change(client, session, world):
    # Dropping a question keeps every answer to it: it becomes stray.
    r = await _update(client, world, questions.wire_all(base_form()[:3]))
    assert r.status_code == 200, r.text
    await session.refresh(world["full"])
    assert world["full"].additional_details["why"] == "Learn"
    out = await ma.summary(session, await session.get(Events, world["event"].id))
    assert {"key": "why", "count": 1} in out["stray"]


async def test_the_summary_skips_a_registration_without_a_live_ticket(session, world):
    regs = (await session.scalars(select(EventRegistrationsLink).where(EventRegistrationsLink.event_id == world["event"].id))).all()
    assert len(regs) == 4  # the deleted one is hidden; the revoked one is a row
    out = await ma.summary(session, world["event"])
    assert out["people"]["registrations"] == 3
