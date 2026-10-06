"""Layer mode for ticket and certificate artwork.

Layers let a club place content on its own artwork instead of restyling the
built-in layout. The parts worth pinning are the ones that fail quietly: a
template with no layers must render exactly as before, and token substitution
must not become a way to run template syntax on the server.
"""

import pytest

from app.api.templates_shared import TemplateLayer, render_layers
from app.api.ticket_templates.schemas import TicketTemplateConfig
from app.api.certificate_templates.schemas import CertificateTemplateConfig


def test_existing_templates_are_untouched():
    """
    Both renderers switch on `layers` being non-empty, so the default has to
    stay empty -- otherwise every club's existing ticket changes on deploy.
    """
    assert TicketTemplateConfig().layers == []
    assert CertificateTemplateConfig().layers == []
    assert TicketTemplateConfig().canvas.width == 0


def test_a_config_from_before_layers_existed_still_parses():
    # Stored configs are JSON written before this field existed.
    old = {"accent_color": "#ff0000", "font": "Inter", "bg_color": "#000000"}
    assert TicketTemplateConfig(**old).layers == []


def test_tokens_are_substituted():
    layers = [TemplateLayer(id="1", text="Hi {{name}}, code {{ticket_code}}")]
    out = render_layers(layers, {"name": "Asha", "ticket_code": "MMP-2048"})
    assert out[0]["rendered_text"] == "Hi Asha, code MMP-2048"


def test_an_unknown_token_is_left_visible():
    """
    Left as written rather than blanked, so a typo shows on the ticket as
    `{{even_name}}`. A silently empty space is the version nobody notices.
    """
    out = render_layers([TemplateLayer(id="1", text="{{even_name}}")], {"event_name": "X"})
    assert out[0]["rendered_text"] == "{{even_name}}"


def test_a_missing_value_becomes_empty_not_the_word_none():
    out = render_layers([TemplateLayer(id="1", text="[{{name}}]")], {"name": None})
    assert out[0]["rendered_text"] == "[]"


def test_layer_text_is_not_evaluated_as_a_template():
    """
    Layer text is club-supplied. Substitution is a plain replace, so template
    syntax in it is printed, never executed -- handing it to Jinja would make
    the editor a way to run code on the server.
    """
    hostile = "{{ 7*7 }}{% for x in range(3) %}x{% endfor %}"
    out = render_layers([TemplateLayer(id="1", text=hostile)], {"name": "Asha"})
    assert out[0]["rendered_text"] == hostile
    assert "49" not in out[0]["rendered_text"]


@pytest.mark.parametrize("field,value", [("font_size", 0), ("max_lines", 0), ("x", -5)])
def test_nonsense_geometry_is_rejected(field, value):
    with pytest.raises(Exception):
        TemplateLayer(**{"id": "1", field: value})


def test_a_qr_layer_keeps_its_size():
    layer = TemplateLayer(id="q", type="qr", width=180, x=40, y=60)
    assert layer.type == "qr" and layer.width == 180
