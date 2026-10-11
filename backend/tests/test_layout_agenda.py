"""The agenda layer's layout, with a fake font where every character is half
an em wide -- the same cases the editor's contract pins, so both sides lay
the run of show out alike."""
from datetime import datetime, timezone

from app.core.layout.engine import AgendaEntry, layout_agenda, sample_agenda
from app.core.layout.model import AgendaLayer


def measure(s: str, size: float) -> float:
    return len(s) * size * 0.5


M = {"title": measure, "detail": measure, "time": measure}
TZ = "Asia/Kolkata"
DAY = datetime(2026, 11, 20, 3, 30, tzinfo=timezone.utc)  # 09:00 IST


def at(h, m=0, day=20):
    return datetime(2026, 11, day, h, m, tzinfo=timezone.utc)


def layer(**more) -> AgendaLayer:
    base = dict(id="agenda", name="Run of show", x=0, y=0, width=600, height=400, size=20, row_gap=10)
    return AgendaLayer(**{**base, **more})


ENTRIES = [
    AgendaEntry(at(3, 30), at(4, 0), "Registration", None, "Foyer"),
    AgendaEntry(at(4, 0), at(5, 0), "Opening keynote", "Ananya Iyer", "Main hall"),
    AgendaEntry(at(5, 0), None, "Group photo", None, None),
]


def test_left_aligned_rows_put_the_time_in_its_own_column_and_the_detail_under_the_title():
    r = layout_agenda(layer(), ENTRIES, TZ, M)
    assert r.dropped == 0 and r.size == 20
    roles = [p.role for p in r.pieces]
    assert roles == ["time", "title", "detail", "time", "title", "detail", "time", "title"]
    time, title, detail = r.pieces[:3]
    assert time.text == "09:00 – 09:30" and time.x == 0
    assert title.text == "Registration" and title.x > time.width
    assert detail.text == "Foyer" and detail.x == title.x and detail.y > title.y
    assert detail.size == 20 * 0.72 and time.size == 20 * 0.78
    # Rows run down the page, times on the title's own baseline.
    assert time.y == title.y
    assert [p.y for p in r.pieces if p.role == "title"] == sorted(p.y for p in r.pieces if p.role == "title")
    assert r.pieces[-1].text == "Group photo" and r.pieces[-2].text == "10:30"


def test_the_detail_joins_speaker_and_room_and_each_can_be_switched_off():
    on = layout_agenda(layer(), ENTRIES[1:2], TZ, M)
    assert [p.text for p in on.pieces if p.role == "detail"] == ["Ananya Iyer · Main hall"]
    no_room = layout_agenda(layer(show_location=False), ENTRIES[1:2], TZ, M)
    assert [p.text for p in no_room.pieces if p.role == "detail"] == ["Ananya Iyer"]
    bare = layout_agenda(layer(show_location=False, show_speaker=False), ENTRIES[1:2], TZ, M)
    assert [p.role for p in bare.pieces] == ["time", "title"]


def test_centre_alignment_stacks_the_time_above_the_title_and_right_mirrors_the_column():
    c = layout_agenda(layer(align="center"), ENTRIES[:1], TZ, M)
    time, title, detail = c.pieces
    assert time.y < title.y < detail.y
    assert abs((time.x + time.width / 2) - 300) < 0.01 and abs((title.x + title.width / 2) - 300) < 0.01
    r = layout_agenda(layer(align="right"), ENTRIES[:1], TZ, M)
    time, title, detail = r.pieces
    assert abs(time.x + time.width - 600) < 0.01
    assert title.x + title.width < time.x


def test_too_much_for_the_box_shrinks_before_it_drops_and_counts_what_did_not_fit():
    many = [AgendaEntry(at(3, i), None, f"Session {i}", "Someone", "Room") for i in range(0, 30)]
    r = layout_agenda(layer(height=300), many, TZ, M)
    assert r.size == 10  # down to half, no further
    assert r.dropped > 0
    more = r.pieces[-1]
    assert more.role == "more" and more.text == f"+ {r.dropped} more sessions"
    assert more.y <= 300
    # Everything drawn sits inside the box.
    assert all(p.y <= 300 + 0.5 for p in r.pieces)
    shown = {p.text for p in r.pieces if p.role == "title"}
    assert len(shown) + r.dropped == 30


def test_two_columns_fill_down_then_across_and_balance_when_everything_fits():
    many = [AgendaEntry(at(3, i), None, f"Session {i}", None, None) for i in range(0, 8)]
    r = layout_agenda(layer(width=800, columns=2, height=400), many, TZ, M)
    assert r.dropped == 0
    xs = sorted({round(p.x) for p in r.pieces if p.role == "time"})
    assert len(xs) == 2 and xs[0] == 0
    # Balanced: four in each column, not eight down the first.
    assert sum(1 for p in r.pieces if p.role == "title" and p.x < 400) == 4


def test_sessions_on_two_days_get_a_heading_each_that_never_ends_a_column():
    two_days = [
        AgendaEntry(at(4, 0), None, "Day one keynote", None, None),
        AgendaEntry(at(4, 0, day=21), None, "Day two keynote", None, None),
    ]
    r = layout_agenda(layer(), two_days, TZ, M)
    days = [p for p in r.pieces if p.role == "day"]
    assert [d.text for d in days] == ["FRIDAY 20 NOVEMBER", "SATURDAY 21 NOVEMBER"]
    assert days[0].size == 20 * 0.7
    assert layout_agenda(layer(show_day_headings=False), two_days, TZ, M).pieces[0].role == "time"


def test_twelve_hour_clocks_and_the_sample_programme():
    r = layout_agenda(layer(), ENTRIES[:1], TZ, M, hour12=True)
    assert r.pieces[0].text == "9:00 AM – 9:30 AM"
    sample = sample_agenda(DAY, TZ)
    assert len(sample) == 6 and sample[0].title == "Registration and coffee"
    assert sample[0].starts_at.isoformat() == "2026-11-20T09:00:00+05:30"
    assert all(e.starts_at < e.ends_at for e in sample)
