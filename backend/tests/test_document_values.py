"""The values a ticket or certificate is filled with.

Three renderers each built their own payload. The ticket read layer tokens
from keys the payload did not have, so the attendee's name was blank on every
layer-mode ticket; the certificate never substituted tokens at all. These
pin one shared answer and, above all, that every token the editor can insert
is a token the document fills.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.api.events.documents import (
    apply_layers,
    canvas_size,
    document_values,
    image_width,
    pdf_page_options,
    qr_data_url,
    sample_values,
)
from app.api.templates_shared import CERTIFICATE_TOKENS, TICKET_TOKENS


@pytest.fixture
async def registered(session, make_club, make_event, make_user, make_registration):
    owner, club = await make_club(name="Yukthi", email="docs@example.com")
    event = await make_event(
        club=club, created_by=owner, name="Build Night",
        event_datetime=datetime(2026, 11, 20, 18, 0, tzinfo=timezone.utc),
        duration=3.0, location_name="Innovation Hall",
    )
    user = await make_user(email="lakshmi@example.com")
    registration = await make_registration(
        event=event, user=user, full_name="Lakshminarayanan Venkataraman",
        position=2, additional_details={"team_name": "Team Nova"},
    )
    await session.flush()
    await session.refresh(registration, ["event"])
    await session.refresh(event, ["club"])
    return registration


@pytest.mark.parametrize("token", TICKET_TOKENS)
async def test_every_ticket_token_the_editor_offers_is_filled(registered, token):
    """A token the editor can insert but the document ignores prints as
    literal braces on the ticket -- which is what event_start_time did."""
    assert token in document_values(registered)


@pytest.mark.parametrize("token", CERTIFICATE_TOKENS)
async def test_every_certificate_token_the_editor_offers_is_filled(registered, token):
    assert token in document_values(registered)


async def test_the_attendee_name_is_the_registration_name(registered):
    """The regression: this came out blank, because the ticket read `name`
    from a payload whose key was `participant_name`."""
    assert document_values(registered)["name"] == "Lakshminarayanan Venkataraman"


async def test_venue_and_club_come_from_the_event(registered):
    values = document_values(registered)

    assert values["venue"] == "Innovation Hall"
    assert values["club_name"] == "Yukthi"
    assert values["event_name"] == "Build Night"


async def test_times_are_derived_from_the_start_and_duration(registered):
    values = document_values(registered)

    assert values["event_start_time"] == "6:00 PM"
    assert values["event_end_time"] == "9:00 PM"
    assert values["event_time"] == "6:00 PM - 9:00 PM"
    assert values["event_date"] == "20 Nov 2026"


async def test_placing_is_spelled_out_for_a_certificate(registered):
    assert document_values(registered)["placing"] == "Second"


async def test_a_team_comes_from_the_custom_field(registered):
    assert document_values(registered)["team_name"] == "Team Nova"


async def test_a_missing_value_is_empty_not_none(
    session, make_club, make_event, make_user, make_registration
):
    """None prints as "None" on a ticket through some paths."""
    owner, club = await make_club(name="Bare", email="bare@example.com")
    event = await make_event(
        club=club, created_by=owner, name="Bare Event",
        event_datetime=datetime.now(timezone.utc) + timedelta(days=1),
        # duration is NOT NULL; zero is "no end time" to the renderer.
        duration=0, location_name=None,
    )
    user = await make_user(email="bare-user@example.com")
    registration = await make_registration(event=event, user=user)
    await session.refresh(registration, ["event"])

    values = document_values(registration)

    assert values["venue"] == ""
    assert values["event_end_time"] == ""
    assert values["placing"] == ""
    assert all(v is not None for v in values.values())


# --- layers --------------------------------------------------------------------


def test_apply_layers_substitutes_into_a_layered_style():
    style = {"layers": [{"id": "a", "type": "text", "text": "Hi {{name}}"}]}

    out = apply_layers(style, {"name": "Aisha"})

    assert out["layers"][0]["rendered_text"] == "Hi Aisha"


def test_apply_layers_leaves_a_built_in_style_alone():
    style = {"bg_color": "#000000", "layers": []}

    assert apply_layers(style, {"name": "x"}) is style


# --- page geometry -------------------------------------------------------------


def test_a_layered_design_prints_on_a_page_its_own_size():
    """A 1560x690 ticket on A4 portrait sat shrunk in the corner of a sheet."""
    style = {"layers": [{"id": "a"}], "canvas": {"width": 1560, "height": 690}}

    options = pdf_page_options(style, default={"page-size": "A4"})

    assert options["page-width"] == "412.75mm"
    assert options["page-height"] == "182.56mm"
    assert "page-size" not in options
    assert "disable-smart-shrinking" in options


def test_a_built_in_design_keeps_its_paper_size():
    options = pdf_page_options({"layers": []}, default={"orientation": "Landscape"})

    assert options == {"orientation": "Landscape"}


def test_the_image_is_rendered_at_the_artwork_width():
    style = {"layers": [{"id": "a"}], "canvas": {"width": 1754, "height": 1240}}

    assert image_width(style, default=1400) == 1754
    assert image_width({"layers": []}, default=1400) == 1400


def test_canvas_size_has_the_renderers_fallback():
    assert canvas_size({"layers": [{"id": "a"}]}) == (1000, 1000)


# --- qr ------------------------------------------------------------------------


def test_the_qr_is_a_png_data_url():
    url = qr_data_url("MOA-1")

    assert url.startswith("data:image/png;base64,")
    assert len(url) > 100


async def test_sample_values_use_the_real_event_but_a_fictional_person(registered):
    values = sample_values(registered.event, "ticket")

    assert values["event_name"] == "Build Night"
    assert values["venue"] == "Innovation Hall"
    assert values["name"] != "Lakshminarayanan Venkataraman"
    assert set(values) == set(document_values(registered))
