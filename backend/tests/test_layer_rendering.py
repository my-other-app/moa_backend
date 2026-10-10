"""The renderer honours every property the designer can set.

The editor and the PDF are two separate implementations of the same
design format, so a property the editor offers and the renderer ignores
looks like a bug in the editor: the organiser moves a slider, the preview
changes, the downloaded ticket does not.

These tests render the real templates rather than asserting on the schema,
because the schema accepting a field proves nothing about the output.
"""
import re

import jinja2
import pytest

TEMPLATES = ("pdf/events/ticket.html", "pdf/events/certificate.html")

_BASE_LAYER = dict(
    rendered_text="", x=0, y=0, width=100, height=None, font_size=12,
    line_spacing=0, max_lines=1, color="#000000", align="left", bold=False,
    hidden=False, locked=False, image_url=None, fill=None, radius=0,
    font=None, weight=None, letter_spacing=0, opacity=100,
    overflow="shrink", type="text",
)


def _layer(**overrides):
    layer = dict(_BASE_LAYER)
    layer.update(overrides)
    return layer


@pytest.fixture
def render():
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(searchpath="templates"), autoescape=True
    )

    def _render(template_name, style):
        return env.get_template(template_name).render(
            style=style,
            qr_code_url="https://qr.example/x.png",
            # The non-layer branch of each template reads these; layer mode
            # does not, but rendering must not depend on that staying true.
            email_payload={}, event={}, user={}, ticket={}, certificate={},
            data={}, context={},
        )

    return _render


@pytest.fixture
def every_property_style():
    """One layer of each type, every new property set to something visible."""
    return dict(
        canvas=dict(width=1000, height=1000),
        background_image_url=None,
        bg_color="#101014",
        layers=[
            _layer(
                id="text", rendered_text="Jane Doe", x=100, y=120, width=600,
                font_size=40, line_spacing=8, max_lines=2, color="#112233",
                align="center", font="Playfair Display", weight=800,
                letter_spacing=7, opacity=55,
            ),
            _layer(
                id="image", type="image", x=40, y=40, width=180, height=90,
                image_url="https://cdn.example.com/sponsor.png", opacity=80,
            ),
            _layer(
                id="shape", type="shape", x=0, y=900, width=1000, height=24,
                fill="#ff5a1f", radius=12, opacity=90,
            ),
            _layer(id="qr", type="qr", x=760, y=760, width=200),
        ],
    )


# Each entry is the CSS the renderer must emit for one editor control.
EXPECTED_CSS = [
    ("a chosen font", "Playfair Display"),
    ("a numeric font weight", "font-weight: 800"),
    ("letter spacing", "letter-spacing: 7px"),
    ("opacity as a fraction", "opacity: 0.55"),
    ("an image layer's source", "sponsor.png"),
    ("an image layer's height", "height: 90px"),
    ("a shape's fill", "#ff5a1f"),
    ("a shape's corner radius", "border-radius: 12px"),
    ("the QR layer", "layer-qr"),
]


@pytest.mark.parametrize("template_name", TEMPLATES)
@pytest.mark.parametrize("label,css", EXPECTED_CSS, ids=[c[0] for c in EXPECTED_CSS])
def test_the_renderer_emits_each_designer_property(
    render, every_property_style, template_name, label, css
):
    html = render(template_name, every_property_style)
    assert css in html, f"{template_name} drops {label}"


@pytest.mark.parametrize("template_name", TEMPLATES)
def test_a_hidden_layer_is_left_off_the_artwork(render, template_name):
    """Hiding a layer parks it in the file; it must not reach the output."""
    style = dict(
        canvas=dict(width=1000, height=1000),
        background_image_url=None, bg_color="#101014",
        layers=[_layer(id="h", rendered_text="PARKED", hidden=True)],
    )

    assert "PARKED" not in render(template_name, style)


@pytest.mark.parametrize("template_name", TEMPLATES)
def test_no_unrendered_jinja_reaches_the_output(
    render, every_property_style, template_name
):
    """A brace split across lines renders as literal text, not as CSS.

    A formatter once reflowed these templates and broke 31 expressions
    that way, which is why no club branding applied to any document for
    months -- the output looked plausible and nothing failed.
    """
    html = render(template_name, every_property_style)

    assert not re.findall(r"\{\{|\{%|\}\}", html)


@pytest.mark.parametrize("template_name", TEMPLATES)
def test_a_design_saved_before_these_properties_existed_still_renders(
    render, template_name
):
    """Configs already in the database have none of the new keys.

    They must render exactly as they did: bold still bold, nothing
    transparent, no crash on the missing fields.
    """
    style = dict(
        canvas=dict(width=1000, height=1000),
        background_image_url=None, bg_color="#101014",
        layers=[
            dict(
                id="a", type="text", rendered_text="Old Design", x=10, y=20,
                width=400, font_size=30, line_spacing=4, max_lines=1,
                color="#ffffff", align="left", bold=True,
            )
        ],
    )

    html = render(template_name, style)

    assert "Old Design" in html
    assert "font-weight: 700" in html
    assert "opacity: 1.0" in html
