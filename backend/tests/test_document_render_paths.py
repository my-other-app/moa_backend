"""The documents, through their real endpoints, with the renderer captured.

wkhtmltopdf is not installed where the tests run, and these are not tests of
it. They stub the two render functions to capture the context the endpoint
built, because that context is where the bugs were: a name read from the
wrong key, tokens never substituted, a QR variable never set, a page size that
ignored the artwork.
"""
from datetime import datetime, timedelta, timezone
import io

import pytest

from app.api.templates_audience import TemplateAudience
from app.api.ticket_templates.models import TicketTemplates
from app.api.certificate_templates.models import CertificateTemplates
from tests.conftest import auth_header

LAYERS = [
    {"id": "n", "type": "text", "text": "{{name}}", "x": 10, "y": 10,
     "width": 600, "font_size": 40, "color": "#fff", "align": "left",
     "bold": False, "max_lines": 1, "line_spacing": 0},
    {"id": "t", "type": "text", "text": "{{event_start_time}} at {{venue}}",
     "x": 10, "y": 80, "width": 600, "font_size": 20, "color": "#fff",
     "align": "left", "bold": False, "max_lines": 1, "line_spacing": 0},
    {"id": "q", "type": "qr", "text": "", "x": 700, "y": 10, "width": 160,
     "font_size": 12, "color": "#fff", "align": "left", "bold": False,
     "max_lines": 1, "line_spacing": 0},
]
CANVAS = {"width": 1560, "height": 690}


@pytest.fixture(autouse=True)
def no_email(monkeypatch):
    from app.core.email import email as email_mod

    monkeypatch.setattr(email_mod, "send_email_with_attachment", lambda **kwargs: None
    )


@pytest.fixture
def captured(monkeypatch):
    """Replace both renderers with stubs that remember what they were given."""
    calls = []

    def fake_pdf(template_path, context, options=None, template_dir=None):
        calls.append(("pdf", template_path, context, options or {}, None))
        return io.BytesIO(b"%PDF-1.4 stub")

    def fake_image(template_path, context, width=1400, template_dir=None):
        calls.append(("image", template_path, context, {}, width))
        return io.BytesIO(b"\x89PNG stub")

    import app.api.events.router as events_router
    import app.api.events.tickets as tickets
    import app.api.events.certificates as certificates
    import app.api.event_designs.router as designs_router

    # The PDFs render in the document modules the router delegates to; the
    # image endpoints still call the renderer from the router.
    monkeypatch.setattr(tickets, "generate_pdf_bytes", fake_pdf)
    monkeypatch.setattr(certificates, "generate_pdf_bytes", fake_pdf)
    monkeypatch.setattr(events_router, "generate_pdf_bytes", fake_pdf)
    monkeypatch.setattr(events_router, "generate_image_bytes", fake_image)
    monkeypatch.setattr(designs_router, "generate_image_bytes", fake_image)
    return calls


@pytest.fixture
async def layered_event(session, make_club, make_event, make_user, make_registration, mint_token):
    """An event with a layered ticket and certificate, one attendee, and a token for each side."""
    owner, club = await make_club(name="Yukthi", email="render@example.com")
    event = await make_event(
        club=club, created_by=owner, name="Build Night",
        event_datetime=datetime(2026, 11, 20, 18, 0, tzinfo=timezone.utc),
        duration=3.0, location_name="Innovation Hall",
    )
    session.add_all([
        TicketTemplates(event_id=event.id, audience=TemplateAudience.event_default,
                        name="T", config={"layers": LAYERS, "canvas": CANVAS}),
        CertificateTemplates(event_id=event.id, audience=TemplateAudience.event_default,
                             name="C", config={"layers": LAYERS, "canvas": CANVAS}),
    ])
    user = await make_user(email="lakshmi@example.com")
    registration = await make_registration(
        event=event, user=user, full_name="Lakshminarayanan Venkataraman",
    )
    registration.is_attended = True
    await session.commit()
    return {
        "event": event,
        "registration": registration,
        "attendee": auth_header(await mint_token(user)),
        "club": auth_header(await mint_token(owner)),
    }


def _layers(context):
    return {l["id"]: l for l in context["style"]["layers"]}


# --- tickets -------------------------------------------------------------------


async def test_the_ticket_pdf_prints_the_attendees_name(client, captured, layered_event):
    """The regression. `{{name}}` was read from a key the payload did not
    have, so this rendered as an empty box on every layer-mode ticket."""
    r = await client.get(
        f"/api/v1/events/tickets/{layered_event['registration'].ticket_id}/pdf",
        headers=layered_event["attendee"],
    )

    assert r.status_code == 200
    kind, _, context, options, _ = captured[-1]
    assert kind == "pdf"
    assert _layers(context)["n"]["rendered_text"] == "Lakshminarayanan Venkataraman"


async def test_the_ticket_fills_tokens_it_used_to_print_as_braces(client, captured, layered_event):
    await client.get(
        f"/api/v1/events/tickets/{layered_event['registration'].ticket_id}/pdf",
        headers=layered_event["attendee"],
    )

    text = _layers(captured[-1][2])["t"]["rendered_text"]
    assert text == "6:00 PM at Innovation Hall"
    assert "{{" not in text


async def test_a_layered_ticket_prints_on_a_page_its_own_size(client, captured, layered_event):
    """1560x690 on A4 portrait sat shrunk in a corner of the sheet."""
    await client.get(
        f"/api/v1/events/tickets/{layered_event['registration'].ticket_id}/pdf",
        headers=layered_event["attendee"],
    )

    options = captured[-1][3]
    assert options["page-width"] == "412.75mm"
    assert options["page-height"] == "182.56mm"


async def test_the_ticket_image_is_the_same_document(client, captured, layered_event):
    """The app showed its own hardcoded card. This is the designed ticket."""
    r = await client.get(
        f"/api/v1/events/tickets/{layered_event['registration'].ticket_id}/image",
        headers=layered_event["attendee"],
    )

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/png")
    kind, template, context, _, width = captured[-1]
    assert kind == "image" and template == "events/ticket.html"
    assert _layers(context)["n"]["rendered_text"] == "Lakshminarayanan Venkataraman"
    assert context["qr_code_url"].startswith("data:image/png;base64,")
    # Rendered at the artwork's width, not a fixed one.
    assert width == 1560


async def test_the_ticket_image_keeps_the_pdfs_access_rule(
    client, captured, layered_event, make_user, mint_token
):
    """A QR is an entry pass; another attendee must not be able to fetch it."""
    stranger = await make_user(email="stranger@example.com")
    r = await client.get(
        f"/api/v1/events/tickets/{layered_event['registration'].ticket_id}/image",
        headers=auth_header(await mint_token(stranger)),
    )

    assert r.status_code == 403
    assert captured == []


async def test_the_owning_club_can_see_the_ticket_image(client, captured, layered_event):
    r = await client.get(
        f"/api/v1/events/tickets/{layered_event['registration'].ticket_id}/image",
        headers=layered_event["club"],
    )

    assert r.status_code == 200


# --- certificates --------------------------------------------------------------


async def test_the_certificate_now_substitutes_its_layers(client, captured, layered_event):
    """Nothing substituted certificate tokens: every text layer was empty."""
    r = await client.get(
        f"/api/v1/events/certificates/{layered_event['registration'].ticket_id}/image"
    )

    assert r.status_code == 200
    context = captured[-1][2]
    assert _layers(context)["n"]["rendered_text"] == "Lakshminarayanan Venkataraman"


async def test_the_certificate_qr_layer_has_a_source(client, captured, layered_event):
    """The template's QR layer reads `qr_code_url`; the context never set it."""
    await client.get(
        f"/api/v1/events/certificates/{layered_event['registration'].ticket_id}/image"
    )

    context = captured[-1][2]
    assert context["qr_code_url"].startswith("data:image/png;base64,")


async def test_a_layered_certificate_pdf_is_sized_to_its_artwork(client, captured, layered_event):
    await client.get(
        f"/api/v1/events/certificates/{layered_event['registration'].ticket_id}/pdf"
    )

    options = captured[-1][3]
    assert options["page-width"] == "412.75mm"
    assert "orientation" not in options


async def test_a_built_in_certificate_keeps_landscape_a4(
    client, captured, session, make_club, make_event, make_user, make_registration
):
    owner, club = await make_club(name="Plain", email="plain@example.com")
    event = await make_event(
        club=club, created_by=owner, name="Plain Event",
        event_datetime=datetime.now(timezone.utc) - timedelta(days=1),
    )
    user = await make_user(email="plain-user@example.com")
    registration = await make_registration(event=event, user=user)
    registration.is_attended = True
    await session.commit()

    await client.get(f"/api/v1/events/certificates/{registration.ticket_id}/pdf")

    assert captured[-1][3] == {"orientation": "Landscape"}
    assert captured[-1][2]["style"].get("layers") in (None, [])


# --- the designer's previews ---------------------------------------------------


async def test_the_designer_can_ask_for_a_real_attendees_values(client, layered_event):
    reg = layered_event["registration"]
    r = await client.get(
        f"/api/v1/events/{layered_event['event'].id}/designs/ticket/sample",
        params={"ticket_id": reg.ticket_id},
        headers=layered_event["club"],
    )

    assert r.status_code == 200
    body = r.json()
    assert body["values"]["name"] == "Lakshminarayanan Venkataraman"
    assert body["values"]["venue"] == "Innovation Hall"
    assert body["qr_data_url"].startswith("data:image/png;base64,")
    assert body["registration"]["ticket_id"] == reg.ticket_id


async def test_without_a_registration_the_sample_is_the_real_event_and_a_fictional_person(
    client, layered_event
):
    r = await client.get(
        f"/api/v1/events/{layered_event['event'].id}/designs/ticket/sample",
        headers=layered_event["club"],
    )

    body = r.json()
    assert body["values"]["event_name"] == "Build Night"
    assert body["values"]["name"] != "Lakshminarayanan Venkataraman"
    assert body["registration"] is None


async def test_a_registration_from_another_event_is_refused(
    client, layered_event, session, make_club, make_event, make_user, make_registration
):
    """Previewing against someone else's attendee by guessing a code."""
    other_owner, other_club = await make_club(name="Other", email="other-render@example.com")
    other_event = await make_event(
        club=other_club, created_by=other_owner, name="Other",
        event_datetime=datetime.now(timezone.utc) + timedelta(days=2),
    )
    someone = await make_user(email="someone@example.com")
    foreign = await make_registration(event=other_event, user=someone)
    await session.commit()

    r = await client.get(
        f"/api/v1/events/{layered_event['event'].id}/designs/ticket/sample",
        params={"ticket_id": foreign.ticket_id},
        headers=layered_event["club"],
    )

    assert r.status_code == 404


async def test_the_exact_preview_renders_the_unsaved_design(client, captured, layered_event):
    """The config comes from the editor, not the database, so a change is
    seen as the renderer will print it before it is saved."""
    reg = layered_event["registration"]
    draft = {"layers": [{**LAYERS[0], "text": "Hello {{name}}"}], "canvas": CANVAS}

    r = await client.post(
        f"/api/v1/events/{layered_event['event'].id}/designs/ticket/preview",
        json={"config": draft, "ticket_id": reg.ticket_id},
        headers=layered_event["club"],
    )

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/png")
    kind, template, context, _, width = captured[-1]
    assert template == "events/ticket.html"
    assert _layers(context)["n"]["rendered_text"] == "Hello Lakshminarayanan Venkataraman"
    assert width == 1560


async def test_the_exact_preview_refuses_a_config_the_api_would_not_store(
    client, captured, layered_event
):
    r = await client.post(
        f"/api/v1/events/{layered_event['event'].id}/designs/ticket/preview",
        json={"config": {"layers": [{"id": "x", "opacity": 500}]}},
        headers=layered_event["club"],
    )

    assert r.status_code == 422
    assert captured == []


async def test_another_club_cannot_preview_this_events_design(
    client, captured, layered_event, make_club, mint_token
):
    other, _ = await make_club(name="Nope", email="nope@example.com")
    r = await client.post(
        f"/api/v1/events/{layered_event['event'].id}/designs/ticket/preview",
        json={"config": {"layers": LAYERS, "canvas": CANVAS}},
        headers=auth_header(await mint_token(other)),
    )

    assert r.status_code == 403
    assert captured == []
