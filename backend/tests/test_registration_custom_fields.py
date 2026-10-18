"""Custom registration fields: which are required, and which are not.

The validator used to reject any field whose key was absent from the
submission without consulting field.required, so `required: False` had no
effect and every custom field was mandatory. A client that correctly omits a
blank optional field -- which the mobile app does -- had its registration
refused with "This field is required" for a field the user was told was
optional.
"""
from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import auth_header

FIELDS = [
    {"key": "tshirt_size", "label": "T-shirt size", "field_type": "select",
     "required": True, "options": ["S", "M", "L"]},
    {"key": "dietary", "label": "Dietary requirements", "field_type": "textarea",
     "required": False, "options": None},
]


@pytest.fixture(autouse=True)
def no_email(monkeypatch):
    """Registration sends a ticket email; nothing here is testing SES."""
    from app.core.email import email as email_mod

    monkeypatch.setattr(email_mod, "send_email_with_attachment", lambda **kwargs: None
    )


@pytest.fixture
async def event_with_fields(session, make_club, make_event):
    owner, club = await make_club(name="Club", email="cf@example.com")
    event = await make_event(
        club=club, created_by=owner, name="Custom Field Event",
        event_datetime=datetime.now(timezone.utc) + timedelta(days=5),
    )
    event.additional_details = FIELDS
    await session.flush()
    return event


async def register(client, token, event_id, details, email):
    # The endpoint requires the submitted email to match the logged-in user.
    return await client.post(
        f"/api/v1/events/registration/{event_id}/register",
        json={
            "full_name": "Test Person",
            "email": email,
            "phone": "9999999999",
            "additional_details": details,
        },
        headers=auth_header(token),
    )


async def test_omitting_an_optional_field_is_allowed(
    client, event_with_fields, make_user, mint_token,
):
    user = await make_user(email="opt1@example.com")
    token = await mint_token(user)

    response = await register(
        client, token, event_with_fields.id, {"tshirt_size": "M"}, user.email
    )

    assert response.status_code == 200, response.text


async def test_a_blank_optional_field_is_allowed(
    client, event_with_fields, make_user, mint_token,
):
    # The app sends only non-empty values, but a blank string must not be
    # treated as an answer either.
    user = await make_user(email="opt2@example.com")
    token = await mint_token(user)

    response = await register(
        client, token, event_with_fields.id, {"tshirt_size": "M", "dietary": "   "}, user.email,
    )

    assert response.status_code == 200, response.text


async def test_omitting_a_required_field_is_still_rejected(
    client, event_with_fields, make_user, mint_token,
):
    user = await make_user(email="req1@example.com")
    token = await mint_token(user)

    response = await register(
        client, token, event_with_fields.id, {"dietary": "none"}, user.email)

    assert response.status_code == 400
    # Named by key in `errors`; `message` is a sentence naming it by label.
    assert "tshirt_size" in response.json()["errors"] and response.json()["error_code"] == "INVALID_ANSWERS"


async def test_a_blank_required_field_is_rejected(
    client, event_with_fields, make_user, mint_token,
):
    user = await make_user(email="req2@example.com")
    token = await mint_token(user)

    response = await register(
        client, token, event_with_fields.id, {"tshirt_size": ""}, user.email
    )

    assert response.status_code == 400


async def test_an_invalid_option_is_rejected(
    client, event_with_fields, make_user, mint_token,
):
    user = await make_user(email="opt3@example.com")
    token = await mint_token(user)

    response = await register(
        client, token, event_with_fields.id, {"tshirt_size": "XXXL"}, user.email
    )

    assert response.status_code == 400
    # Named by key in `errors`; `message` is a sentence naming it by label.
    assert "tshirt_size" in response.json()["errors"] and response.json()["error_code"] == "INVALID_ANSWERS"


async def test_the_optional_answer_is_stored_when_given(
    client, session, event_with_fields, make_user, mint_token,
):
    user = await make_user(email="opt4@example.com")
    token = await mint_token(user)

    response = await register(
        client, token, event_with_fields.id, {"tshirt_size": "L", "dietary": "Vegetarian"}, user.email,
    )

    assert response.status_code == 200, response.text
    from sqlalchemy import select

    from app.api.events.models import EventRegistrationsLink

    registration = await session.scalar(
        select(EventRegistrationsLink).where(
            EventRegistrationsLink.event_id == event_with_fields.id,
            EventRegistrationsLink.user_id == user.id,
        )
    )
    assert registration.additional_details["dietary"] == "Vegetarian"
    assert registration.additional_details["tshirt_size"] == "L"
