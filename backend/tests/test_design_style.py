"""A design's colours, fonts, border and layers go into the ticket's and the
certificate's CSS: each has a shape, checked when it is saved and again when
it is drawn (app/api/templates_style.py)."""
import re

import pytest
from pydantic import ValidationError

from app.api import templates_style as ts
from app.api.certificate_templates.schemas import CertificateTemplateConfig
from app.api.templates_shared import TemplateLayer
from app.api.ticket_templates.schemas import TicketTemplateConfig
from app.core.utils.pdf import render_html
from tests.conftest import auth_header

INJECTED = "red; background: url(http://169.254.169.254/latest/meta-data)"
PIXEL = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
PAYLOAD = {
    "ticket_id": "MOA-1", "certificate_no": "C-1", "participant_name": "Asha", "club_name": "Palette Club",
    "event_name": "Build Night", "event_date": "1 Dec 2026", "event_time": "6:00 PM", "event_location": "Hall A",
    "event_prizes": None, "qr_code": "",
}


# First in the file: a run that starts with a test opening no database sets
# the suite's loop up apart from the session's engine (tests/conftest.py).
@pytest.fixture
async def design_world(client, session, make_club, make_event, mint_token):
    owner, club = await make_club(name="Palette Club", email="palette@example.com")
    event = await make_event(club=club, created_by=owner, name="Colour Night")
    await session.commit()
    return dict(event=event, h=auth_header(await mint_token(owner)))


async def test_saving_an_event_design_checks_what_goes_into_css(client, design_world):
    url = f"/api/v1/events/{design_world['event'].id}/designs/ticket"
    bad = await client.put(url, json={"config": {"accent_color": INJECTED}}, headers=design_world["h"])
    assert bad.status_code == 422 and bad.json()["error_code"] == "INVALID_DESIGN", bad.text
    assert any("accent_color" in p for p in bad.json()["errors"]["config"])
    bad = await client.put(url, json={"config": {"border_style": "dotted; x: url(y)"}}, headers=design_world["h"])
    assert bad.status_code == 422
    ok = await client.put(url, json={"config": {"accent_color": "#1DB9A0", "bg_color": "#121212", "border_style": "dashed", "font": "Inter"}}, headers=design_world["h"])
    assert ok.status_code == 200, ok.text
    assert ok.json()["config"]["accent_color"] == "#1DB9A0"


def test_the_shapes():
    for good in ("#1db9a0", "#1DB9A0", "#fff", "#000000"):
        assert ts.colour_ok(good)
    for bad in ("red", "#12345", "#1db9a0;", "rgb(0,0,0)", INJECTED, "#1db9a0 url(x)", "", None, 7):
        assert not ts.colour_ok(bad)
    assert ts.font_ok("Noto Sans JP") and ts.font_ok("Inter")
    for bad in ('Inter"); @import url(x', "Inter;", "a" * 65, ""):
        assert not ts.font_ok(bad)


def test_a_design_that_is_not_one_is_named_field_by_field():
    found = ts.problems({
        "accent_color": INJECTED, "bg_color": "#121212", "border_style": "solid; background: url(x)",
        "font": 'Inter"); x', "layers": [{"color": "red", "fill": "#fff", "x": "1px; top: url(x)"}],
    })
    assert any(p.startswith("accent_color") for p in found)
    assert any(p.startswith("border_style") for p in found)
    assert any(p.startswith("font ") for p in found)
    assert any(p.startswith("layers[0].color") for p in found) and any(p.startswith("layers[0].x") for p in found)
    assert not any("bg_color" in p or "fill" in p for p in found)
    assert ts.problems({"accent_color": "#1db9a0", "border_style": "dashed", "font": "Inter", "text_color": None, "layout": {"any": "thing"}}) == []


def test_the_template_schemas_refuse_them():
    with pytest.raises(ValidationError):
        TicketTemplateConfig(accent_color=INJECTED)
    with pytest.raises(ValidationError):
        TicketTemplateConfig(border_style="solid; background: url(x)")
    with pytest.raises(ValidationError):
        CertificateTemplateConfig(title_font='Inter"); x')
    with pytest.raises(ValidationError):
        TemplateLayer(id="a", color=INJECTED)
    with pytest.raises(ValidationError):
        TemplateLayer(id="a", type="shape", fill="url(x)")
    assert TicketTemplateConfig(accent_color="#123456", border_style="double").accent_color == "#123456"
    assert TemplateLayer(id="a", color="#fff", fill=None).color == "#fff"


def test_a_stored_design_is_made_safe_when_it_is_drawn():
    """Designs saved before the check exist: what is not of its shape is left
    out, and the template's own default prints."""
    style = {
        "accent_color": INJECTED, "bg_color": "#222222", "text_color": "#ffffff", "border_style": "solid; x: url(y)",
        "border_color": "#1db9a0", "font": 'Inter"); @import url(http://x', "logo_url": "http://127.0.0.1:8000/logo.png",
        "background_image_url": PIXEL,
    }
    html = render_html("events/ticket.html", {"email_payload": PAYLOAD, "style": style, "qr_code_url": PIXEL})
    assert "169.254.169.254" not in html and "127.0.0.1" not in html and "@import url(http://x" not in html
    assert "#222222" in html and "#1db9a0" in html  # the hex colours still print
    assert PIXEL in html
    # Every url() left in the CSS is the fonts' or an embedded picture.
    for target in re.findall(r"url\(\s*['\"]?([^'\")]+)", html):
        assert target.startswith(("https://fonts.googleapis.com/", "data:image/")), target


def test_a_layered_certificate_drops_what_it_cannot_draw_safely():
    style = {
        "layers": [
            {"id": "t", "type": "text", "text": "Hi", "x": 10, "y": 20, "font_size": 24, "line_spacing": 0, "max_lines": 1, "color": INJECTED, "align": "left; x: url(y)"},
            {"id": "i", "type": "image", "image_url": "http://127.0.0.1:8000/x.png", "x": 0, "y": 0},
            {"id": "s", "type": "shape", "fill": "#ff0000", "x": "1px; background: url(http://x)", "y": 0},
        ],
        "canvas": {"width": 1000, "height": "700px; x"},
    }
    from app.api.events.documents import apply_layers

    html = render_html("events/certificate.html", {"email_payload": PAYLOAD, "style": apply_layers(style, {}), "qr_code_url": PIXEL})
    assert "url(http" not in html and "127.0.0.1" not in html and "169.254" not in html
    assert "#ff0000" in html and "Hi" in html
