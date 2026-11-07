"""Conditional registration questions (feature-register/conditional-questions-contract.md).

A question may be shown only when an earlier choice question was answered a
certain way (`show_if`). Every case here uses the contract's shared test
form (section 12), the same form every repo tests:

    1 status       radio  [Student, Working]  required
    2 college      text                       required  show_if status in [Student]
    3 year         select [1, 2, 3, 4]        required  show_if status in [Student]
    4 degree_type  radio  [UG, PG]            required  show_if status in [Student]
    5 pg_spec      text                       required  show_if degree_type in [PG]   (nested)
    6 company      text                       required  show_if status in [Working]
    7 experience   select [0-2, 3-5, 5+]      required  show_if status in [Working]
    8 tshirt       select [S, M, L, XL]       required

- Which questions are shown, and what is kept when the answers are sent
  (sections 3 and 4): hidden answers are dropped, a hidden question is never
  required, and an old app that answers everything is pruned to what shows.
- The rules a form keeps on create and update, R1-R6 (section 2).
- The wire format (section 5): every read of a form, by anyone, sends a
  question with `show_if` as `required: false` plus `required_when_shown`;
  a write takes `required_when_shown` over `required`. The crawl at the end
  calls every GET route the API has as a stranger, an attendee, the club and
  an admin, so a read path added later that sends a form its own way fails
  here.
- Duplication keeps the conditions; the readers (export, approvals, check-in,
  closeout, MCP) take a question hidden for someone as simply unanswered.
- X-App-Version is accepted and kept for the request's logs (section 10).
"""
import io
import json
import logging
from datetime import datetime, timedelta, timezone

import pytest
from openpyxl import load_workbook

from app.api.closeout.models import EventStatus
from app.api.events import questions
from app.api.events.models import EventCategories, EventRegistrationsLink
from app.api.events.registration import answers
from app.core.rate_limit import limiter
from app.response import CustomHTTPException
from tests.conftest import auth_header

#: Every test opens the suite's session, the ones that never touch the
#: database too: run alone, a first test without it left the session's
#: connection on another event loop than the next test's.
pytestmark = pytest.mark.usefixtures("session")

STUDENT = {"key": "status", "any_of": ["Student"]}
WORKING = {"key": "status", "any_of": ["Working"]}


def _q(key, label, kind, options=None, show_if=None, required=True):
    q = {"key": key, "label": label, "field_type": kind, "required": required, "options": options}
    if show_if is not None:
        q["show_if"] = show_if
    return q


def shared_form() -> list[dict]:
    """The contract's section 12 form, as it is stored."""
    return [
        _q("status", "Are you a student or working?", "radio", ["Student", "Working"]),
        _q("college", "College name", "text", show_if=STUDENT),
        _q("year", "Year of study", "select", ["1", "2", "3", "4"], show_if=STUDENT),
        _q("degree_type", "Degree type", "radio", ["UG", "PG"], show_if=STUDENT),
        _q("pg_spec", "PG specialisation", "text", show_if={"key": "degree_type", "any_of": ["PG"]}),
        _q("company", "Company", "text", show_if=WORKING),
        _q("experience", "Experience", "select", ["0-2", "3-5", "5+"], show_if=WORKING),
        _q("tshirt", "T-shirt size", "select", ["S", "M", "L", "XL"]),
    ]


FORM = shared_form()
CONDITIONAL = {q["key"] for q in FORM if q.get("show_if")}
EVERY_ANSWER = {
    "status": "Working", "college": "IIT Madras", "year": "2", "degree_type": "PG", "pg_spec": "VLSI",
    "company": "Acme", "experience": "3-5", "tshirt": "M",
}


def _shown(answers_):
    return {k for k, v in questions.visibility(FORM, answers_).items() if v}


def _refusal(fn, *args, **kwargs) -> CustomHTTPException:
    with pytest.raises(CustomHTTPException) as e:
        fn(*args, **kwargs)
    assert e.value.status_code == 400
    return e.value


# ─── Visibility and submitting (sections 3 and 4) ───────────────────────────


async def test_student_ug_shows_questions_one_to_four_and_eight():
    sent = {"status": "Student", "college": "NIT Calicut", "year": "3", "degree_type": "UG", "tshirt": "L"}
    assert _shown(sent) == {"status", "college", "year", "degree_type", "tshirt"}
    # 5-7 hidden, so not required: the answers are enough.
    assert answers.check(FORM, sent) == sent


async def test_student_pg_reveals_the_nested_question_and_requires_it():
    sent = {"status": "Student", "college": "NIT Calicut", "year": "3", "degree_type": "PG", "tshirt": "L"}
    assert "pg_spec" in _shown(sent)
    refused = _refusal(answers.check, FORM, sent)
    assert refused.errors == {"pg_spec": "This field is required"}
    assert "PG specialisation" in refused.message
    assert answers.check(FORM, {**sent, "pg_spec": "VLSI"})["pg_spec"] == "VLSI"


async def test_working_shows_six_to_eight_and_hides_two_to_five():
    sent = {"status": "Working", "company": "Acme", "experience": "5+", "tshirt": "S"}
    assert _shown(sent) == {"status", "company", "experience", "tshirt"}
    assert answers.check(FORM, sent) == sent


async def test_switching_student_to_working_drops_the_student_answers():
    # The student questions were filled, then the answer became Working.
    sent = {"status": "Working", "college": "IIT Madras", "year": "2", "degree_type": "PG", "pg_spec": "VLSI",
            "company": "Acme", "experience": "0-2", "tshirt": "M"}
    kept = answers.check(FORM, sent)
    assert kept == {"status": "Working", "company": "Acme", "experience": "0-2", "tshirt": "M"}


async def test_switching_pg_to_ug_drops_only_the_pg_answer():
    sent = {"status": "Student", "college": "IIT Madras", "year": "2", "degree_type": "UG", "pg_spec": "VLSI", "tshirt": "M"}
    kept = answers.check(FORM, sent)
    assert kept == {"status": "Student", "college": "IIT Madras", "year": "2", "degree_type": "UG", "tshirt": "M"}


async def test_a_hidden_parent_hides_its_children_whatever_they_answer():
    # degree_type answered PG, but status is Working: degree_type is hidden, so pg_spec is too.
    assert "pg_spec" not in _shown({"status": "Working", "degree_type": "PG"})
    assert "pg_spec" not in _shown({"degree_type": "PG"})


async def test_an_old_app_answering_everything_is_pruned_and_the_shown_ones_still_enforced():
    # An old app shows every question flat: everything answered, the hidden ones dropped.
    assert answers.check(FORM, EVERY_ANSWER) == {"status": "Working", "company": "Acme", "experience": "3-5", "tshirt": "M"}
    # It left a shown required one blank: refused, naming only that one; the
    # blank hidden ones are not mentioned.
    sent = {**EVERY_ANSWER, "company": "", "college": "", "pg_spec": ""}
    refused = _refusal(answers.check, FORM, sent)
    assert refused.errors == {"company": "This field is required"}
    assert refused.message == "Check your answers: Company."


async def test_a_hidden_answer_of_the_wrong_type_is_dropped_not_refused():
    kept = answers.check(FORM, {"status": "Working", "company": "Acme", "experience": "5+", "tshirt": "S", "year": "seven"})
    assert "year" not in kept


async def test_nothing_answered_asks_only_for_the_always_shown():
    refused = _refusal(answers.check, FORM, {})
    assert set(refused.errors) == {"status", "tshirt"}


async def test_the_door_relaxed_still_drops_hidden_answers_and_requires_nothing():
    kept = answers.check(FORM, {**EVERY_ANSWER, "company": "", "organization": "Acme Corp"}, relaxed=True)
    # Blanks dropped, nothing required, hidden answers gone, a key the form doesn't ask kept.
    assert kept == {"status": "Working", "experience": "3-5", "tshirt": "M", "organization": "Acme Corp"}
    assert answers.check(FORM, {}, relaxed=True) == {}


async def test_a_form_without_conditions_is_checked_as_before():
    form = [_q("tshirt", "T-shirt", "select", ["S", "M"]), _q("diet", "Diet", "text", required=False)]
    assert answers.check(form, {"tshirt": "S"}) == {"tshirt": "S"}
    assert set(_refusal(answers.check, form, {"diet": "veg"}).errors) == {"tshirt"}


# ─── The rules a form keeps (section 2, R1-R6) ──────────────────────────────
#
# The sentences are the dashboards' (moa_web packages/questions/src/rules.ts),
# word for word: a club reads the same one whichever side catches it.


async def test_the_shared_form_is_a_valid_form():
    questions.check_form(FORM)


def _chain(levels: int) -> list[dict]:
    """An always-shown question and `levels` questions below it, each shown by the one before."""
    form = [_q("l0", "Level 0", "radio", ["Yes", "No"])]
    for n in range(1, levels + 1):
        form.append(_q(f"l{n}", f"Level {n}", "radio", ["Yes", "No"], show_if={"key": f"l{n - 1}", "any_of": ["Yes"]}))
    return form


def _without_pg():
    form = [dict(q) for q in FORM]
    form[3] = {**form[3], "options": ["UG"]}
    return form


CANONICAL = {
    # R1
    "child_before_parent": (
        lambda: [FORM[1], FORM[0], *FORM[2:]], None, "college",
        "'College name' depends on 'Are you a student or working?', which must come before it.",
    ),
    "cycle": (
        lambda: [_q("a", "First", "radio", ["x", "y"], show_if={"key": "b", "any_of": ["x"]}),
                 _q("b", "Second", "radio", ["x", "y"], show_if={"key": "a", "any_of": ["x"]})], None, "a",
        "'First' depends on 'Second', which must come before it.",
    ),
    "itself": (
        lambda: [_q("a", "First", "radio", ["x", "y"], show_if={"key": "a", "any_of": ["x"]})], None, "a",
        "'First' can't depend on itself.",
    ),
    "missing_parent": (
        lambda: [_q("c", "College", "text", show_if={"key": "nope", "any_of": ["x"]})], None, "c",
        "'College' depends on a question that isn't in this form.",
    ),
    # R2
    "text_parent": (
        lambda: [_q("role", "Your role", "text"), _q("c", "College", "text", show_if={"key": "role", "any_of": ["Student"]})], None, "c",
        "'College' depends on 'Your role', which isn't a choice question. Only a question with options can show or hide others.",
    ),
    "lone_tick_box": (
        lambda: [_q("agree", "I agree", "checkbox"), _q("why", "Why", "text", show_if={"key": "agree", "any_of": ["true"]})], None, "why",
        "'Why' depends on 'I agree', which isn't a choice question. Only a question with options can show or hide others.",
    ),
    # R3
    "empty_any_of": (
        lambda: [FORM[0], _q("college", "College name", "text", show_if={"key": "status", "any_of": []})], None, "college",
        "'College name' needs at least one answer to 'Are you a student or working?' that shows it.",
    ),
    "not_an_option": (
        lambda: [FORM[0], _q("college", "College name", "text", show_if={"key": "status", "any_of": ["Retired"]})], None, "college",
        "'College name' is shown when 'Are you a student or working?' is 'Retired', which isn't one of its options.",
    ),
    # R4
    "depth_six": (
        lambda: _chain(6), None, "l6",
        "'Level 6' is nested 6 deep. Questions can be nested at most 5 deep.",
    ),
    "fifty_one": (
        lambda: [_q(f"q{n}", f"Question {n}", "text") for n in range(51)], None, "additional_details",
        "A form can ask at most 50 questions; this one has 51.",
    ),
    # R5
    "same_key": (
        lambda: [_q("a", "First", "text"), _q("a", "Second", "text")], None, "a",
        "'Second' has the same key as 'First'. Every question needs its own.",
    ),
    # R6
    "pg_removed": (
        _without_pg, FORM, "pg_spec",
        "'PG' can't be removed or renamed: 'PG specialisation' is shown when 'Degree type' is 'PG'. Change that question's condition first.",
    ),
}


@pytest.mark.parametrize("case", sorted(CANONICAL))
async def test_a_refused_form_says_the_dashboards_sentence_naming_the_question(case):
    make, before, key, sentence = CANONICAL[case]
    refused = _refusal(questions.check_form, make(), before=before)
    assert refused.message == sentence
    assert refused.errors == {key: sentence}
    assert refused.error_code == "INVALID_FORM"


async def test_a_question_without_a_label_is_named_by_its_key():
    form = [_q("role", "", "text"), _q("c", "  ", "text", show_if={"key": "role", "any_of": ["x"]})]
    assert _refusal(questions.check_form, form).message.startswith("'c' depends on 'role', which isn't a choice question.")


async def test_depth_counts_the_conditions_above_a_question():
    # An always-shown question is 0, its child 1: five conditions deep is kept.
    questions.check_form(_chain(5))
    assert "is nested 6 deep" in _refusal(questions.check_form, _chain(6)).message


async def test_a_renamed_option_reads_as_r6_and_changing_the_child_with_it_is_fine():
    renamed = [dict(q) for q in FORM]
    renamed[3] = {**renamed[3], "options": ["UG", "Postgraduate"]}
    assert _refusal(questions.check_form, renamed, before=FORM).message.startswith("'PG' can't be removed or renamed: 'PG specialisation'")
    both = [dict(q) for q in renamed]
    both[4] = {**both[4], "show_if": {"key": "degree_type", "any_of": ["Postgraduate"]}}
    questions.check_form(both, before=FORM)
    # A new child naming an option that never was: R3, not R6.
    assert "which isn't one of its options" in _refusal(questions.check_form, renamed, before=[FORM[0], FORM[3]]).message


async def test_the_same_key_is_named_before_any_condition_problem():
    form = [_q("c", "College", "text", show_if={"key": "nope", "any_of": ["x"]}), _q("a", "First", "text"), _q("a", "Second", "text")]
    assert _refusal(questions.check_form, form).message == "'Second' has the same key as 'First'. Every question needs its own."


# ─── Matching is exact (section 3) ──────────────────────────────────────────


@pytest.mark.parametrize("answer, shown", [
    ("Student", True), ("student", False), (" Student", False), ("Student ", False), ("Working", False),
    (None, False), ("", False), ("   ", False), ([], False), (["Student"], True), (["  "], False),
])
async def test_a_choice_answer_matches_exactly(answer, shown):
    assert questions.visibility(FORM, {"status": answer})["college"] is shown


@pytest.mark.parametrize("answer, shown", [
    (["AI", "Hardware"], True), ("Hardware", True), (["AI", "Web"], False), ("hardware", False),
    ([], False), ("", False), (None, False), ([" ", None], False),
])
async def test_a_checkbox_answer_matches_as_a_list_or_one_string(answer, shown):
    form = [
        _q("tracks", "Tracks", "checkbox", ["AI", "Web", "Hardware"]),
        _q("kit", "Kit size", "select", ["Small", "Large"], show_if={"key": "tracks", "any_of": ["Hardware"]}),
    ]
    assert questions.visibility(form, {"tracks": answer})["kit"] is shown


async def test_a_number_answer_matches_its_option_as_text():
    form = [_q("year", "Year", "select", ["1", "2"]), _q("hostel", "Hostel", "text", show_if={"key": "year", "any_of": ["1"]})]
    assert questions.visibility(form, {"year": 1})["hostel"] is True
    assert questions.visibility(form, {"year": "1"})["hostel"] is True
    assert questions.visibility(form, {"year": 2})["hostel"] is False


# ─── Through the API: create, update, the wire format ───────────────────────


@pytest.fixture(autouse=True)
def _no_ticket_mail(monkeypatch):
    from app.core.email import email as email_mod

    monkeypatch.setattr(email_mod, "send_email_with_attachment", lambda **kwargs: None)


@pytest.fixture
async def world(session, make_club, make_user, make_admin, mint_token, monkeypatch):
    monkeypatch.setattr(limiter, "enabled", False)
    owner, club = await make_club(name="Branching Club", email="branching@example.com")
    category = EventCategories(name="Workshops")
    session.add(category)
    await session.flush()
    attendee = await make_user(email="attendee@example.com")
    admin = await make_admin(email="cq-admin@example.com")
    await session.commit()
    form = {
        "name": "Branching Night",
        "event_datetime": (datetime.now(timezone.utc) + timedelta(days=9)).isoformat(), "duration": "2",
        "has_fee": "false", "status": "published", "category_id": str(category.id),
    }
    return dict(
        owner=owner, club=club, attendee=attendee, admin=admin, form=form,
        h=auth_header(await mint_token(owner)), attendee_h=auth_header(await mint_token(attendee)),
        admin_h=auth_header(await mint_token(admin)),
    )


async def _create(client, world, details, **extra):
    return await client.post(
        "/api/v1/events/create", data={**world["form"], "additional_details": json.dumps(details), **extra}, headers=world["h"],
    )


async def _info(client, event_id, headers=None) -> list[dict]:
    r = await client.get(f"/api/v1/events/info/{event_id}", headers=headers or {})
    assert r.status_code == 200, r.text
    return r.json()["additional_details"]


def _assert_wire(sent: list[dict]):
    """Every question as the contract's section 5 has it on a read."""
    by_key = {q["key"]: q for q in sent}
    assert list(by_key) == [q["key"] for q in FORM]
    for q in FORM:
        out = by_key[q["key"]]
        if q.get("show_if"):
            assert out["required"] is False, out
            assert out["required_when_shown"] is q["required"], out
            assert out["show_if"] == q["show_if"], out
        else:
            assert out["required"] is q["required"], out
            assert "required_when_shown" not in out and out.get("show_if") is None, out


async def test_a_form_with_conditions_is_stored_as_the_club_set_it_and_sent_downgraded(client, session, world):
    made = await _create(client, world, FORM)
    assert made.status_code == 200, made.text
    # The create's own answer is a read of the form too.
    _assert_wire(made.json()["additional_details"])
    event_id = made.json()["id"]

    from app.api.events.models import Events

    stored = (await session.get(Events, event_id)).additional_details
    assert stored == FORM  # required as the club set it; no required_when_shown kept

    _assert_wire(await _info(client, event_id))
    _assert_wire(await _info(client, event_id, world["h"]))
    _assert_wire(await _info(client, event_id, world["admin_h"]))


async def test_a_write_takes_required_when_shown_over_required(client, session, world):
    from app.api.events.models import Events

    # What a new editor sends back after reading: the downgraded form.
    sent = questions.wire_all(FORM)
    sent[2] = {**sent[2], "required_when_shown": False}  # the club made "Year of study" optional
    made = await _create(client, world, sent)
    assert made.status_code == 200, made.text
    stored = (await session.get(Events, made.json()["id"])).additional_details
    assert [q["required"] for q in stored] == [True, True, False, True, True, True, True, True]
    assert all("required_when_shown" not in q for q in stored)


async def test_a_round_trip_through_an_editor_keeps_show_if_and_required_when_shown(client, session, world):
    from app.api.events.models import Events

    made = await _create(client, world, FORM)
    event_id = made.json()["id"]
    read = await _info(client, event_id, world["h"])
    # An editor that saves what it read, untouched.
    saved = await client.put(
        f"/api/v1/events/update/{event_id}", data={**world["form"], "additional_details": json.dumps(read)}, headers=world["h"],
    )
    assert saved.status_code == 200, saved.text
    await session.refresh(await session.get(Events, event_id))
    assert (await session.get(Events, event_id)).additional_details == FORM
    _assert_wire(await _info(client, event_id))


async def test_an_old_editor_that_sends_required_alone_is_still_read(client, session, world):
    from app.api.events.models import Events

    made = await _create(client, world, FORM)
    assert made.status_code == 200
    plain = [{k: v for k, v in q.items() if k != "show_if"} for q in FORM]
    plain[0] = {**plain[0], "required": False}
    saved = await client.put(
        f"/api/v1/events/update/{made.json()['id']}", data={**world["form"], "additional_details": json.dumps(plain)}, headers=world["h"],
    )
    assert saved.status_code == 200, saved.text
    stored = (await session.get(Events, made.json()["id"])).additional_details
    assert stored[0]["required"] is False and all("show_if" not in q for q in stored)


@pytest.mark.parametrize(
    "case",
    ["child_before_parent", "cycle", "text_parent", "unknown_value", "depth_six"],
)
async def test_a_bad_form_is_refused_on_create_with_a_400_naming_the_question(client, world, case):
    bad = {
        "child_before_parent": [FORM[1], FORM[0]],
        "cycle": [
            _q("a", "First", "radio", ["x"], show_if={"key": "b", "any_of": ["x"]}),
            _q("b", "Second", "radio", ["x"], show_if={"key": "a", "any_of": ["x"]}),
        ],
        "text_parent": [_q("t", "Your role", "text"), _q("c", "College name", "text", show_if={"key": "t", "any_of": ["x"]})],
        "unknown_value": [FORM[0], _q("college", "College name", "text", show_if={"key": "status", "any_of": ["Retired"]})],
        "depth_six": _chain(6),
    }[case]
    r = await _create(client, world, bad)
    assert r.status_code == 400, r.text
    body = r.json()
    assert body["error_code"] == "INVALID_FORM"
    named = {"child_before_parent": "College name", "cycle": "First", "text_parent": "College name", "unknown_value": "College name", "depth_six": "Level 6"}[case]
    assert f"'{named}'" in body["message"]


async def test_removing_pg_on_update_is_refused_naming_pg_specialisation(client, session, world):
    made = await _create(client, world, FORM)
    without_pg = [dict(q) for q in FORM]
    without_pg[3] = {**without_pg[3], "options": ["UG"]}
    r = await client.put(
        f"/api/v1/events/update/{made.json()['id']}", data={**world["form"], "additional_details": json.dumps(without_pg)}, headers=world["h"],
    )
    assert r.status_code == 400, r.text
    assert "'PG specialisation'" in r.json()["message"] and r.json()["errors"] == {"pg_spec": r.json()["message"]}
    from app.api.events.models import Events

    assert (await session.get(Events, made.json()["id"])).additional_details == FORM  # nothing changed


async def _register(client, world, event_id, details):
    return await client.post(
        f"/api/v1/events/registration/{event_id}/register",
        json={"full_name": "Asha Rao", "email": world["attendee"].email, "phone": "9876543210", "additional_details": details},
        headers=world["attendee_h"],
    )


async def test_registering_with_an_old_app_keeps_only_the_shown_answers(client, session, world):
    made = await _create(client, world, FORM)
    event_id = made.json()["id"]
    r = await _register(client, world, event_id, EVERY_ANSWER)
    assert r.status_code == 200, r.text
    from sqlalchemy import select

    reg = await session.scalar(select(EventRegistrationsLink).where(EventRegistrationsLink.event_id == event_id))
    assert reg.additional_details == {"status": "Working", "company": "Acme", "experience": "3-5", "tshirt": "M"}


async def test_registering_with_a_shown_required_answer_blank_is_refused_naming_it(client, world):
    made = await _create(client, world, FORM)
    r = await _register(client, world, made.json()["id"], {"status": "Student", "college": "NIT", "year": "1", "degree_type": "PG", "tshirt": "S"})
    assert r.status_code == 400, r.text
    assert r.json()["errors"] == {"pg_spec": "This field is required"}
    assert "PG specialisation" in r.json()["message"]


# ─── Duplication and the readers (sections 6 and 9) ─────────────────────────


async def test_duplicating_an_event_keeps_its_conditions(client, session, world):
    from app.api.events.duplicate import duplicate_event
    from app.api.events.models import Events

    source = await session.get(Events, (await _create(client, world, FORM)).json()["id"])
    out = await duplicate_event(
        session, source, world["owner"], name="Branching Night II", event_datetime=datetime.now(timezone.utc) + timedelta(days=30),
    )
    copy_id = out["id"] if isinstance(out, dict) and "id" in out else out["event"]["id"]
    assert (await session.get(Events, copy_id)).additional_details == FORM


async def _registered(session, make_registration, event, user, details, **extra):
    return await make_registration(event=event, user=user, additional_details=details, **extra)


@pytest.fixture
async def answered(client, session, world, make_user, make_registration):
    """The form's event with a student and a worker registered, each with only their own branch answered."""
    from app.api.events.models import Events

    event = await session.get(Events, (await _create(client, world, FORM)).json()["id"])
    student = await make_user(email="student@example.com")
    worker = await make_user(email="worker@example.com")
    s = await _registered(session, make_registration, event, student,
                          {"status": "Student", "college": "NIT Calicut", "year": "2", "degree_type": "PG", "pg_spec": "VLSI", "tshirt": "S"})
    w = await _registered(session, make_registration, event, worker, {"status": "Working", "company": "Acme", "experience": "5+", "tshirt": "L"})
    await session.commit()
    return dict(event=event, student=s, worker=w)


async def test_the_export_leaves_a_hidden_question_blank_in_form_order(client, world, answered):
    r = await client.get(f"/api/v1/events/registration/{answered['event'].id}/export", headers=world["h"])
    assert r.status_code == 200, r.text
    rows = list(load_workbook(io.BytesIO(r.content)).active.iter_rows(values_only=True))
    header = list(rows[0])
    assert header[4:12] == [q["key"] for q in FORM]
    by_email = {row[2]: dict(zip(header, row)) for row in rows[1:]}
    worker = by_email["worker@example.com"]
    assert worker["company"] == "Acme" and worker["college"] in (None, "") and worker["pg_spec"] in (None, "")
    student = by_email["student@example.com"]
    assert student["pg_spec"] == "VLSI" and student["company"] in (None, "")


async def test_the_attendee_detail_shows_only_what_each_answered(client, world, answered):
    r = await client.get(f"/api/v1/events/registration/{answered['event'].id}/info/{answered['worker'].id}", headers=world["h"])
    assert r.status_code == 200, r.text
    assert r.json()["additional_details"] == {"status": "Working", "company": "Acme", "experience": "5+", "tshirt": "L"}


async def test_an_organiser_switching_an_answer_drops_the_branch_it_hides(client, session, world, answered):
    event_id, reg = answered["event"].id, answered["student"]
    r = await client.patch(
        f"/api/v1/events/registration/{event_id}/info/{reg.id}", json={"additional_details": {"degree_type": "UG"}}, headers=world["h"],
    )
    assert r.status_code == 200, r.text
    await session.refresh(reg)
    assert reg.additional_details == {"status": "Student", "college": "NIT Calicut", "year": "2", "degree_type": "UG", "tshirt": "S"}


async def _live(session, event_id):
    from app.api.events.models import Events

    event = await session.get(Events, event_id)
    event.lifecycle = EventStatus.live
    event.event_datetime = datetime.now(timezone.utc) - timedelta(minutes=10)
    await session.commit()


async def test_a_walk_up_at_the_door_drops_hidden_answers(client, session, world):
    from sqlalchemy import select

    event_id = (await _create(client, world, FORM)).json()["id"]
    await _live(session, event_id)
    r = await client.post(
        f"/api/v1/events/{event_id}/gate/walkup",
        json={"name": "Ravi Kumar", "email": "ravi@example.com", "client_ref": "walkup-cq-0001",
              "custom_fields": {"status": "Working", "college": "IIT", "company": "Acme"}},
        headers=world["h"],
    )
    assert r.status_code in (200, 201), r.text
    reg = await session.scalar(select(EventRegistrationsLink).where(
        EventRegistrationsLink.event_id == event_id, EventRegistrationsLink.email == "ravi@example.com",
    ))
    # Nothing is required at the door, and the student answer is gone.
    assert reg.additional_details == {"status": "Working", "company": "Acme"}


async def test_the_check_in_console_gets_the_form_downgraded(client, session, world):
    event_id = (await _create(client, world, FORM)).json()["id"]
    r = await client.get(f"/api/v1/events/{event_id}/gate/context", headers=world["h"])
    assert r.status_code == 200, r.text
    _assert_wire(r.json()["guest_fields"])


@pytest.fixture
def mcp_on(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "MCP_ENABLED", True)
    monkeypatch.setattr(settings, "MCP_RATE_LIMIT", "60/minute;2000/day")


async def test_the_mcp_tools_send_the_form_downgraded_and_answers_as_given(client, world, answered, mcp_on):
    from tests.test_mcp import call, connect

    c = await connect(client, "branching@example.com")
    got = await call(client, c.access_token, "get_event", {"event_id": answered["event"].id})
    by_key = {q["key"]: q for q in got["questions"]}
    assert list(by_key) == [q["key"] for q in FORM]
    for q in FORM:
        if q.get("show_if"):
            assert by_key[q["key"]]["required"] is False and by_key[q["key"]]["required_when_shown"] is True
            assert by_key[q["key"]]["show_if"] == q["show_if"]
        else:
            assert by_key[q["key"]]["required"] is True and "show_if" not in by_key[q["key"]]
    listed = await call(client, c.access_token, "list_registrations", {"event_id": answered["event"].id})
    assert "_error" not in listed, listed
    asked = [{a["question"] for a in p["answers"]} for p in listed["registrations"]]
    # Each person's own branch only: a question hidden for them is simply not there.
    assert sorted(map(sorted, asked)) == sorted(map(sorted, [
        {"Are you a student or working?", "College name", "Year of study", "Degree type", "PG specialisation", "T-shirt size"},
        {"Are you a student or working?", "Company", "Experience", "T-shirt size"},
    ]))


async def test_mcp_writes_a_condition_by_the_earlier_question_s_label_and_keeps_the_rules(client, session, world, mcp_on):
    from app.api.events.models import Events
    from tests.test_mcp import call, connect

    c = await connect(client, "branching@example.com")
    made = await call(client, c.access_token, "create_event_draft", {
        "name": "Branching Draft", "starts_at": (datetime.now(timezone.utc) + timedelta(days=20)).isoformat(), "duration_hours": 2,
        "questions": [
            {"label": "Are you a student or working?", "type": "radio", "options": ["Student", "Working"]},
            {"label": "College name", "type": "text", "show_if": {"question": "are you a student or working?", "any_of": ["Student"]}},
        ],
    })
    assert "_error" not in made, made
    event_id = made.get("id") or made.get("event_id") or made["event"]["id"]
    stored = (await session.get(Events, event_id)).additional_details
    assert stored[1]["show_if"] == {"key": "are_you_a_student_or_working", "any_of": ["Student"]}
    assert stored[1]["required"] is True

    refused = await call(client, c.access_token, "update_event_draft", {
        "event_id": event_id,
        "questions": [
            {"label": "Role", "type": "text"},
            {"label": "College name", "type": "text", "show_if": {"question": "Role", "any_of": ["Student"]}},
        ],
    })
    assert "isn't a choice question" in json.dumps(refused)
    later = await call(client, c.access_token, "update_event_draft", {
        "event_id": event_id, "questions": [{"label": "College name", "type": "text", "show_if": {"question": "Later", "any_of": ["x"]}}],
    })
    assert "_error" in later


async def test_closeout_and_the_college_lookup_take_a_hidden_question_as_unanswered(world, answered):
    # The analytics' college: the student's answer; the worker was never asked.
    assert answered["student"].institution == "NIT Calicut"
    assert answered["worker"].institution is None
    from app.api.events import documents

    # The ticket's tokens: a hidden question is an empty value, never an error.
    assert documents.token_attendee(answered["worker"]).custom_fields.get("college") is None


async def test_approvals_keep_only_the_shown_answers(client, session, world):
    from sqlalchemy import select

    from app.api.approvals.models import RegistrationRequests
    from app.api.events.models import Events

    event_id = (await _create(client, world, FORM, requires_approval="true")).json()["id"]
    r = await client.post(
        f"/api/v1/events/registration/{event_id}/register",
        json={"full_name": "Asha Rao", "email": world["attendee"].email, "phone": "9876543210", "additional_details": EVERY_ANSWER, "accepts_approval": True},
        headers=world["attendee_h"],
    )
    assert r.status_code in (200, 201, 202), r.text
    req = await session.scalar(select(RegistrationRequests).where(RegistrationRequests.event_id == event_id))
    assert req.answers == {"status": "Working", "company": "Acme", "experience": "3-5", "tshirt": "M"}
    assert (await session.get(Events, event_id)).requires_approval is True


# ─── X-App-Version (section 10) ─────────────────────────────────────────────


async def test_x_app_version_is_accepted_and_changes_nothing(client, world):
    url = f"/api/v1/events/info/{(await _create(client, world, FORM)).json()['id']}"
    plain = await client.get(url)
    versioned = await client.get(url, headers={"X-App-Version": "1.2.0+7"})
    junk = await client.get(url, headers={"X-App-Version": "<script>alert(1)</script>"})
    assert plain.status_code == versioned.status_code == junk.status_code == 200
    assert plain.json()["additional_details"] == versioned.json()["additional_details"] == junk.json()["additional_details"]


async def test_x_app_version_is_on_every_log_line_the_request_causes():
    from app.core import observability

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(observability.RequestIdFilter())
    handler.setFormatter(observability.JsonFormatter())
    probe = logging.getLogger("app.cq_probe")
    probe.addHandler(handler)
    probe.setLevel(logging.INFO)
    seen = []

    async def app(scope, receive, send):
        seen.append(observability.current_app_version())
        probe.info("handled")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        pass

    middleware = observability.AppVersionMiddleware(app)
    try:
        for headers in ([(b"x-app-version", b"1.1.0+6")], [(b"x-app-version", b"<script>")], []):
            await middleware({"type": "http", "method": "GET", "path": "/", "headers": headers}, receive, send)
    finally:
        probe.removeHandler(handler)
    assert seen == ["1.1.0+6", None, None]
    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert lines[0]["app_version"] == "1.1.0+6"
    assert "app_version" not in lines[1] and "app_version" not in lines[2]
    assert observability.current_app_version() is None


# ─── Every read path (section 5): the crawl ─────────────────────────────────

#: Routes the crawl must see the form on: if one stops carrying it (or moves),
#: the list is updated on purpose. A new read path that carries the form is
#: checked by the crawl whether or not it is listed here.
FORM_READ_PATHS = {
    "/api/v1/events/info/{event_id}",  # the app, the website, the dashboards' edit forms
    "/api/v1/events/{event_id}/gate/context",  # the check-in console's walk-up form
}

#: Never called: they stream (server-sent events) or render files.
_SKIP = ("stream", "/sse", ".pdf", "/pdf", "/image", ".pkpass", ".png", ".svg", ".ics", ".csv", "/export", "/download", "/preview")


def _question_like(node) -> bool:
    return isinstance(node, dict) and "key" in node and "label" in node and ("field_type" in node or "type" in node)


def _questions_in(node, out: list):
    if isinstance(node, dict):
        if _question_like(node) and node.get("key") in CONDITIONAL:
            out.append(node)
        for v in node.values():
            _questions_in(v, out)
    elif isinstance(node, list):
        for v in node:
            _questions_in(v, out)
    return out


async def test_every_read_of_a_form_sends_conditional_questions_as_not_required(client, session, world, make_registration, mcp_on):
    from tests.test_cohost_routes import _spec, sample

    made = await _create(client, world, FORM)
    assert made.status_code == 200, made.text
    event_id, slug = made.json()["id"], made.json()["slug"]
    from app.api.events.models import Events

    event = await session.get(Events, event_id)
    reg = await make_registration(event=event, user=world["attendee"], additional_details={"status": "Working", "company": "Acme", "experience": "5+", "tshirt": "L"})
    await session.commit()
    club = world["club"]
    by_name = {
        "event_id": event_id, "id": event_id, "slug": slug, "event_slug": slug, "slug_or_id": slug, "club_id": club.id, "club_slug": club.slug,
        "handle": club.slug, "ref": slug, "ticket_id": reg.ticket_id, "registration_id": str(reg.id), "user_id": world["attendee"].id,
    }
    identities = {"anyone": {}, "attendee": world["attendee_h"], "club": world["h"], "admin": world["admin_h"]}
    found: dict[str, set] = {}
    problems, called = [], 0
    spec = _spec()
    for path, ops in sorted(spec["paths"].items()):
        op = ops.get("get")
        if op is None or any(s in path for s in _SKIP):
            continue
        url, query, skip = path, {}, False
        for p in op.get("parameters", []):
            if p["in"] == "path":
                if p["name"] not in by_name:
                    skip = True
                    break
                url = url.replace("{" + p["name"] + "}", str(by_name[p["name"]]))
            elif p["in"] == "query" and p.get("required"):
                query[p["name"]] = sample(p.get("schema", {}))
        if skip:
            continue
        called += 1
        for who, headers in identities.items():
            try:
                r = await client.get(url, params=query, headers=headers)
            except Exception:  # noqa: BLE001 (a route that needs what the suite lacks)
                await session.rollback()
                continue
            if r.status_code != 200 or "json" not in r.headers.get("content-type", ""):
                continue
            for q in _questions_in(r.json(), []):
                found.setdefault(path, set()).add(who)
                if q.get("required") is not False or q.get("required_when_shown") is not True or not q.get("show_if"):
                    problems.append(f"{who} GET {path}: {q}")
    assert called > 200, called  # the crawl reached the API, not a handful of routes
    assert not problems, "\n".join(problems)
    assert FORM_READ_PATHS <= set(found), sorted(FORM_READ_PATHS - set(found))
