"""Long names fit the box instead of being cut off.

Artwork does not reflow, so a name longer than the box it was designed for
has to either shrink or be clipped. It was clipped, which is how a long name
reached a certificate with its end missing.

The width estimate is an approximation, so these tests assert on behaviour --
it shrank, it stayed readable, it did not touch a layer that already fit --
rather than on exact pixel sizes, which would break on any tuning of the
character table.
"""
import pytest

from app.api.templates_fit import estimate_text_width, fit_font_size, lines_needed
from app.api.templates_shared import render_layers

SHORT = "Ann Lee"
LONG = "Lakshminarayanan Venkataraman"


def test_a_name_that_fits_is_left_at_its_chosen_size():
    assert fit_font_size(SHORT, font_size=40, box_width=600, max_lines=1) == 40


def test_a_name_too_long_for_its_box_is_shrunk():
    fitted = fit_font_size(LONG, font_size=40, box_width=300, max_lines=1)

    assert fitted < 40


def test_shrinking_stops_while_the_text_is_still_readable():
    """Past a point, clipping is the more honest failure than 6px type."""
    fitted = fit_font_size(LONG, font_size=40, box_width=20, max_lines=1)

    assert fitted >= 8
    assert fitted >= 40 * 0.4


def test_the_fitted_size_actually_fits():
    box = 300
    fitted = fit_font_size(LONG, font_size=40, box_width=box, max_lines=1)

    assert lines_needed(LONG, box, fitted) <= 1


def test_more_lines_allow_a_larger_size():
    """A two-line box should not be shrunk as hard as a one-line box."""
    one = fit_font_size(LONG, font_size=40, box_width=300, max_lines=1)
    two = fit_font_size(LONG, font_size=40, box_width=300, max_lines=2)

    assert two > one


def test_a_layer_with_no_box_is_never_shrunk():
    """Width is optional, and without it there is nothing to fit against."""
    assert fit_font_size(LONG, font_size=40, box_width=None, max_lines=1) == 40


@pytest.mark.parametrize("text", ["", "   "])
def test_empty_text_is_left_alone(text):
    assert fit_font_size(text, font_size=40, box_width=10, max_lines=1) == 40


def test_wide_characters_are_estimated_wider_than_narrow_ones():
    """A flat average puts 'WWWW' and 'iiii' badly wrong in opposite directions."""
    assert estimate_text_width("WWWW", 20) > estimate_text_width("iiii", 20) * 2


def test_bold_text_is_estimated_wider():
    assert estimate_text_width(LONG, 20, bold=True) > estimate_text_width(LONG, 20)


def test_letter_spacing_counts_toward_the_width():
    spaced = estimate_text_width(SHORT, 20, letter_spacing=5)

    assert spaced > estimate_text_width(SHORT, 20)
    # One gap fewer than characters, not one per character.
    assert spaced == pytest.approx(
        estimate_text_width(SHORT, 20) + 5 * (len(SHORT) - 1)
    )


def test_a_single_word_wider_than_the_box_is_one_line_not_many():
    """The renderer does not break words, so neither does the estimate."""
    assert lines_needed("Lakshminarayanan", 10, 40) == 1


# --- render_layers ------------------------------------------------------------


def _layer(**overrides):
    layer = dict(
        id="a", type="text", text="{{name}}", x=0, y=0, width=300,
        font_size=40, max_lines=1, line_spacing=0, color="#000",
        align="left", bold=False, overflow="shrink",
    )
    layer.update(overrides)
    return layer


def test_render_layers_shrinks_the_substituted_text_not_the_token():
    """`{{name}}` is short; the name it stands for may not be.

    Fitting has to happen after substitution or it measures the wrong string
    -- which would leave exactly the layers that overflow untouched.
    """
    rendered = render_layers([_layer()], {"name": LONG})[0]

    assert rendered["rendered_text"] == LONG
    assert rendered["rendered_font_size"] < 40


def test_render_layers_leaves_a_short_name_at_full_size():
    rendered = render_layers([_layer()], {"name": SHORT})[0]

    assert rendered["rendered_font_size"] == 40


def test_a_layer_set_to_clip_is_not_shrunk():
    """Clipping is a choice: some designs would rather cut than resize."""
    rendered = render_layers([_layer(overflow="clip")], {"name": LONG})[0]

    assert rendered["rendered_font_size"] == 40


def test_a_config_saved_before_fitting_existed_still_renders():
    """Old layers have no `overflow` key and must not raise."""
    legacy = dict(
        id="a", type="text", text="{{name}}", x=0, y=0, width=300,
        font_size=40, max_lines=1, line_spacing=0, color="#000",
        align="left", bold=True,
    )

    rendered = render_layers([legacy], {"name": LONG})[0]

    assert rendered["rendered_font_size"] <= 40


@pytest.mark.parametrize("layer_type", ["qr", "image", "shape"])
def test_only_text_layers_are_fitted(layer_type):
    """A QR code's size is its size; shrinking it would make it unscannable."""
    rendered = render_layers(
        [_layer(type=layer_type, text="", width=160)], {"name": LONG}
    )[0]

    assert rendered["rendered_font_size"] == 40


# --- agreement with the editor -------------------------------------------------
#
# The editor shrinks text too, in `moa_web/apps/clubs/src/features/templates/
# text-fit.ts`, which is a port of this module. If the two disagree the preview
# lies about what the PDF will contain.
#
# The same case table is checked on both sides -- here, and in
# `text-fit.test.ts` next to that port. Tuning the character widths on either
# side fails that side's copy of this test, which is what points whoever is
# tuning at the other one.


def _shared_cases():
    import json
    import pathlib

    path = pathlib.Path(__file__).parent / "fixtures" / "text_fit_cases.json"
    return json.loads(path.read_text())


@pytest.mark.parametrize("case", _shared_cases(), ids=lambda c: c["text"][:18])
def test_the_shared_cases_fit_as_the_editor_expects(case):
    fitted = fit_font_size(
        case["text"],
        font_size=case["fontSize"],
        box_width=case["boxWidth"],
        max_lines=case["maxLines"],
        bold=case["bold"],
        letter_spacing=case["letterSpacing"],
    )

    assert fitted == case["expected"]


@pytest.mark.parametrize("case", _shared_cases(), ids=lambda c: c["text"][:18])
def test_the_shared_cases_wrap_as_the_editor_expects(case):
    wrapped = lines_needed(
        case["text"],
        case["boxWidth"],
        case["fontSize"],
        bold=case["bold"],
        letter_spacing=case["letterSpacing"],
    )

    assert wrapped == case["expectedLines"]
