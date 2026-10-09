"""The PDF/image templates render their Jinja expressions.

A code formatter reflowed `{{ expr }}` across lines in both templates --
17 in certificate.html, 14 in ticket.html -- leaving `{` and `{` on
separate lines. Jinja only treats them as an expression when adjacent, so
each one rendered as literal, invalid CSS and the declaration was thrown
away.

Every one was a style value, so no club's colours had ever applied to a
certificate or a ticket. Nothing failed: the documents generated fine,
just unstyled, which is why it went unnoticed.

These tests fail loudly if a formatter does it again.
"""
import re
from pathlib import Path

import pytest

TEMPLATES = Path(__file__).resolve().parents[1] / "templates" / "pdf" / "events"

# `{` then whitespace-with-a-newline then `{` -- what the reflow leaves.
SPLIT_EXPRESSION = re.compile(r"\{\s*\n\s*\{")


@pytest.mark.parametrize("name", ["certificate.html", "ticket.html"])
def test_no_jinja_expression_is_split_across_lines(name):
    source = (TEMPLATES / name).read_text()

    offenders = SPLIT_EXPRESSION.findall(source)

    assert not offenders, (
        f"{name} has {len(offenders)} Jinja expressions split across lines. "
        "Jinja needs the braces adjacent; split, they render as literal CSS "
        "and the declaration is discarded."
    )


@pytest.mark.parametrize("name", ["certificate.html", "ticket.html"])
def test_the_template_still_parses(name):
    import jinja2

    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(searchpath=str(TEMPLATES.parent)),
        autoescape=True,
    )

    env.get_template(f"events/{name}")


def test_a_styled_certificate_renders_its_colours():
    import jinja2

    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(searchpath=str(TEMPLATES.parent)),
        autoescape=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )

    class Style:
        accent_color = "#1db9a0"
        bg_color = "#121212"
        text_color = "#ffffff"
        secondary_text_color = "#aaaaaa"
        border_color = "#1db9a0"
        font = "Inter"
        certificate_title = "Certificate of Participation"
        orientation = "landscape"
        show_qr_code = True
        show_certificate_id = True
        layers = []
        background_image_url = None
        logo_url = None
        secondary_logo_url = None
        custom_body_text = None
        custom_footer_text = None
        signature_1_name = None
        signature_2_name = None
        signature_1_title = None
        signature_2_title = None
        signature_1_url = None
        signature_2_url = None
        title_font = None
        border_style = "none"

    html = env.get_template("events/certificate.html").render(
        email_payload={
            "ticket_id": "MOA-TEST",
            "participant_name": "Muhammed R",
            "event_name": "quick",
            "event_date": "31 Jan 2026",
            "organizer_name": "Yukthi",
            "qr_code": "",
        },
        style=Style(),
    )

    # The club's colours reach the stylesheet, rather than being discarded
    # as unparseable.
    assert "#1db9a0" in html
    assert "#121212" in html
    assert "#ffffff" in html
    # And the content is there.
    assert "Muhammed R" in html
    assert "Certificate of Participation" in html
