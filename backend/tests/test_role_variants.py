"""A speaker receives the speaker design.

The variants were reachable from the designer and from the resolver, but the
three places that actually render a document never passed a role -- so every
variant a club designed would have sat unused while everyone received the
event default. That is the failure these cover: not whether resolution works,
but whether the thing that renders asks for the right one.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.api.certificate_templates.models import CertificateTemplates
from app.api.events.router import _role_of
from app.api.templates_audience import RegistrationRole, TemplateAudience
from app.api.ticket_templates.models import TicketTemplates
from app.api.ticket_templates.service import get_template_for_event
from tests.conftest import auth_header


@pytest.fixture
async def event_with_variants(session, make_club, make_event):
    """An event whose speakers get a different ticket from everyone else."""
    owner, club = await make_club(name="Yukthi", email="roles@example.com")
    event = await make_event(
        club=club, created_by=owner, name="Dev Summit",
        event_datetime=datetime.now(timezone.utc) + timedelta(days=6),
    )

    session.add_all([
        TicketTemplates(
            event_id=event.id,
            audience=TemplateAudience.event_default,
            name="Everyone",
            config={"bg_color": "#default0"},
        ),
        TicketTemplates(
            event_id=event.id,
            audience=TemplateAudience.speaker,
            name="Speakers",
            config={"bg_color": "#speaker0"},
        ),
    ])
    await session.commit()
    return owner, club, event


async def test_a_speaker_gets_the_speaker_design(session, event_with_variants):
    _, _, event = event_with_variants

    config = await get_template_for_event(
        session, event.id, role=RegistrationRole.speaker
    )

    assert config["bg_color"] == "#speaker0"


async def test_a_role_with_no_variant_gets_the_event_default(
    session, event_with_variants
):
    _, _, event = event_with_variants

    config = await get_template_for_event(
        session, event.id, role=RegistrationRole.staff
    )

    assert config["bg_color"] == "#default0"


async def test_no_role_gets_the_event_default(session, event_with_variants):
    _, _, event = event_with_variants

    config = await get_template_for_event(session, event.id, role=None)

    assert config["bg_color"] == "#default0"


# --- what the renderer reads off a registration -------------------------------


class _Registration:
    """Just the one attribute `_role_of` looks at."""

    def __init__(self, role):
        self.role = role


@pytest.mark.parametrize(
    "stored,expected",
    [
        (RegistrationRole.speaker, RegistrationRole.speaker),
        ("speaker", RegistrationRole.speaker),
        ("staff", RegistrationRole.staff),
        (None, None),
    ],
)
def test_the_renderer_reads_the_role_however_it_is_stored(stored, expected):
    """The column may hand back a member or a plain string.

    `use_enum_values` is set on some models, so both shapes reach this, and
    only one of them would have worked if it compared types.
    """
    assert _role_of(_Registration(stored)) is expected


def test_an_unknown_role_falls_back_rather_than_failing_the_render():
    """A role this build does not know must not stop a ticket downloading."""
    assert _role_of(_Registration("keynote")) is None


def test_a_registration_with_no_role_column_still_renders():
    """Guest registrations and older rows may not carry one at all."""

    class Bare:
        pass

    assert _role_of(Bare()) is None


# --- setting the role ---------------------------------------------------------


@pytest.fixture
async def organiser(session, event_with_variants, mint_token):
    owner, club, event = event_with_variants
    token = await mint_token(owner)
    await session.commit()
    return event, auth_header(token)


async def test_an_organiser_can_mark_someone_a_speaker(
    client, session, organiser, make_user, make_registration
):
    event, headers = organiser
    attendee = await make_user(email="speaker@example.com")
    registration = await make_registration(event=event, user=attendee)
    await session.commit()

    response = await client.put(
        f"/api/v1/events/registration/{event.id}/role/{registration.ticket_id}",
        headers=headers,
        json={"role": "speaker"},
    )

    assert response.status_code == 200
    await session.refresh(registration)
    assert _role_of(registration) is RegistrationRole.speaker


async def test_marking_a_speaker_changes_which_design_they_receive(
    client, session, organiser, make_user, make_registration
):
    """The point of the endpoint, asserted end to end."""
    event, headers = organiser
    attendee = await make_user(email="speaker2@example.com")
    registration = await make_registration(event=event, user=attendee)
    await session.commit()

    before = await get_template_for_event(
        session, event.id, role=_role_of(registration)
    )
    await client.put(
        f"/api/v1/events/registration/{event.id}/role/{registration.ticket_id}",
        headers=headers,
        json={"role": "speaker"},
    )
    await session.refresh(registration)
    after = await get_template_for_event(
        session, event.id, role=_role_of(registration)
    )

    # An attendee with no attendee variant falls through to the event
    # default, so the design only changes once they become a speaker.
    assert before["bg_color"] == "#default0"
    assert after["bg_color"] == "#speaker0"


async def test_clearing_a_role_resets_it_to_attendee(
    client, session, organiser, make_user, make_registration
):
    """There is no "attending as nobody": the column is not nullable.

    Sending null means "they are an ordinary attendee again", which is what
    every registration starts as.
    """
    event, headers = organiser
    attendee = await make_user(email="speaker3@example.com")
    registration = await make_registration(
        event=event, user=attendee, role=RegistrationRole.speaker
    )
    await session.commit()

    response = await client.put(
        f"/api/v1/events/registration/{event.id}/role/{registration.ticket_id}",
        headers=headers,
        json={"role": None},
    )

    assert response.status_code == 200
    await session.refresh(registration)
    assert _role_of(registration) is RegistrationRole.attendee


async def test_an_unknown_role_is_rejected(
    client, session, organiser, make_user, make_registration
):
    event, headers = organiser
    attendee = await make_user(email="speaker4@example.com")
    registration = await make_registration(event=event, user=attendee)
    await session.commit()

    response = await client.put(
        f"/api/v1/events/registration/{event.id}/role/{registration.ticket_id}",
        headers=headers,
        json={"role": "keynote"},
    )

    assert response.status_code == 422


async def test_another_club_cannot_set_roles_on_this_event(
    client, session, organiser, make_user, make_registration, make_club, mint_token
):
    event, _ = organiser
    attendee = await make_user(email="speaker5@example.com")
    registration = await make_registration(event=event, user=attendee)
    other_owner, _ = await make_club(name="Other", email="otherroles@example.com")
    other_token = await mint_token(other_owner)
    await session.commit()

    response = await client.put(
        f"/api/v1/events/registration/{event.id}/role/{registration.ticket_id}",
        headers=auth_header(other_token),
        json={"role": "speaker"},
    )

    assert response.status_code == 403


async def test_certificates_resolve_by_role_too(session, event_with_variants):
    """Tickets and certificates must not diverge on this."""
    _, _, event = event_with_variants
    from app.api.certificate_templates.service import (
        get_template_for_event as certificate_for_event,
    )

    session.add_all([
        CertificateTemplates(
            event_id=event.id,
            audience=TemplateAudience.event_default,
            name="Everyone",
            config={"bg_color": "#certdef"},
        ),
        CertificateTemplates(
            event_id=event.id,
            audience=TemplateAudience.speaker,
            name="Speakers",
            config={"bg_color": "#certspk"},
        ),
    ])
    await session.commit()

    config = await certificate_for_event(
        session, event.id, role=RegistrationRole.speaker
    )

    assert config["bg_color"] == "#certspk"


async def test_the_registration_list_reports_each_role(
    client, session, organiser, make_user, make_registration
):
    """The dashboard cannot offer to change a role it cannot see.

    The role was on the row and resolved by the renderer, but neither
    response serialised it -- so the attendee list had no way to show who
    was a speaker, which is where an organiser sets it.
    """
    event, headers = organiser
    speaker = await make_user(email="listed-speaker@example.com")
    await make_registration(
        event=event, user=speaker, role=RegistrationRole.speaker
    )
    plain = await make_user(email="listed-attendee@example.com")
    await make_registration(event=event, user=plain)
    await session.commit()

    response = await client.get(
        f"/api/v1/events/registration/{event.id}/list", headers=headers
    )

    assert response.status_code == 200
    roles = {row["email"]: row["role"] for row in response.json()["items"]}
    assert roles["listed-speaker@example.com"] == "speaker"
    assert roles["listed-attendee@example.com"] == "attendee"


# --- bulk import --------------------------------------------------------------


@pytest.mark.parametrize(
    "cell,expected",
    [
        ("speaker", RegistrationRole.speaker),
        ("Speaker", RegistrationRole.speaker),
        ("  STAFF  ", RegistrationRole.staff),
        ("attendee", RegistrationRole.attendee),
    ],
)
def test_a_role_cell_is_read_forgivingly(cell, expected):
    """A human filled this spreadsheet in, so "Speaker " has to work.

    Failing somebody's whole row over capitalisation would be a poor trade
    for strictness nobody asked for.
    """
    from app.api.events.registration.service import _role_from_sheet

    assert _role_from_sheet(cell) is expected


@pytest.mark.parametrize("cell", [None, "", "   ", "nan", "none"])
def test_a_sheet_with_no_role_column_imports_as_before(cell):
    """The column is optional: an older sheet must import unchanged."""
    from app.api.events.registration.service import _role_from_sheet

    assert _role_from_sheet(cell) is None


def test_an_unreadable_role_still_imports_the_person():
    """Getting people registered matters more than the variant they get.

    Rejecting the row would lose a registration over a typo in a column
    that only picks which artwork they receive.
    """
    from app.api.events.registration.service import _role_from_sheet

    assert _role_from_sheet("keynote speaker") is None


async def test_bulk_import_registers_a_speaker_as_a_speaker(
    session, event_with_variants, make_user
):
    """End to end: the sheet says speaker, the row comes out a speaker."""
    from app.api.events.registration.service import register_event

    _, _, event = event_with_variants
    user = await make_user(email="imported@example.com")
    await session.commit()

    await register_event(
        session=session,
        full_name="Imported Speaker",
        email=user.email,
        user_id=user.id,
        event_id=event.id,
        role=RegistrationRole.speaker,
    )

    from sqlalchemy import select

    from app.api.events.models import EventRegistrationsLink

    registration = await session.scalar(
        select(EventRegistrationsLink).where(
            EventRegistrationsLink.event_id == event.id,
            EventRegistrationsLink.user_id == user.id,
        )
    )
    assert _role_of(registration) is RegistrationRole.speaker

    config = await get_template_for_event(
        session, event.id, role=_role_of(registration)
    )
    assert config["bg_color"] == "#speaker0"


async def test_registering_without_a_role_still_works(
    session, event_with_variants, make_user
):
    """Every other caller of register_event passes no role at all.

    The column is not nullable, so this has to fall to the default rather
    than writing None.
    """
    from app.api.events.registration.service import register_event

    _, _, event = event_with_variants
    user = await make_user(email="ordinary@example.com")
    await session.commit()

    await register_event(
        session=session,
        full_name="Ordinary Attendee",
        email=user.email,
        user_id=user.id,
        event_id=event.id,
    )

    from sqlalchemy import select

    from app.api.events.models import EventRegistrationsLink

    registration = await session.scalar(
        select(EventRegistrationsLink).where(
            EventRegistrationsLink.event_id == event.id,
            EventRegistrationsLink.user_id == user.id,
        )
    )
    assert _role_of(registration) is RegistrationRole.attendee
