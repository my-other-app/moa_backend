"""Text layout, with a fake font where every character is half an em wide --
the same cases the editor's contract pins, so both sides wrap alike."""
import pytest

from app.core.layout.engine import layout_text, line_x
from app.core.layout.model import TextLayer


def measure(s: str, size: float) -> float:
    return len(s) * size * 0.5


BASE = dict(width=200, size=20, max_lines=1, line_spacing=0, letter_spacing=0, fit="shrink", font_file="inter-400", measure=measure)


def test_keeps_a_short_line_as_it_is():
    r = layout_text("Amara", **BASE)
    assert r.size == 20
    assert r.lines == [{"text": "Amara", "width": 50}]
    assert r.truncated is False


def test_shrinks_a_long_name_onto_its_lines_before_cutting_anything():
    r = layout_text("Amara Okonjo-Lindqvist", **BASE)  # 22 chars x 10 = 220 > 200
    assert len(r.lines) == 1
    assert r.size < 20
    assert r.lines[0]["width"] <= 200
    assert r.truncated is False


def test_wraps_when_allowed_and_cuts_with_an_ellipsis_only_past_half_size():
    two = layout_text("one two three four five six", **{**BASE, "max_lines": 2, "fit": "truncate"})
    assert [l["text"] for l in two.lines] == ["one two three four", "five six"]
    assert two.baselines[1] - two.baselines[0] == pytest.approx(24)
    cut = layout_text("x" * 100, **{**BASE, "fit": "truncate"})
    assert cut.truncated is True
    assert cut.lines[0]["text"].endswith("…")
    assert cut.lines[0]["width"] <= 200
    shrunk = layout_text("x" * 100, **BASE)
    assert shrunk.size == 10
    assert shrunk.truncated is True


def test_breaks_a_single_long_word_honours_line_breaks_and_spaces_lines_as_asked():
    r = layout_text("abcdefghijklmnopqrstuvwxy\nz", **{**BASE, "max_lines": 4, "fit": "truncate", "line_spacing": 6})
    assert [l["text"] for l in r.lines] == ["abcdefghijklmnopqrst", "uvwxy", "z"]
    assert r.baselines[1] - r.baselines[0] == pytest.approx(30)


def test_places_lines_by_alignment():
    def layer(align):
        return TextLayer(id="t", name="t", x=10, y=0, text="", width=200, size=20, align=align)

    assert line_x(layer("left"), 50) == 10
    assert line_x(layer("center"), 50) == 85
    assert line_x(layer("right"), 50) == 160
