"""The quiz's scoring engine (app/api/live/scoring.py), the round and poll
settings and the theme's contrast rules, as pure functions: every rule of
CONTRACT.md sections 1.2, 3 and 4, with its worked examples.

Written as async tests (no database) so they share the suite's event loop."""

import random

import pytest

from app.api.live import names, scoring, theme
from app.api.live import settings as live_settings
from app.response import CustomHTTPException

DEFAULT = live_settings.quiz_settings(None)["scoring"]


@pytest.fixture(autouse=True)
def _same_loop_as_the_suite(_engine):
    """No database here, but the suite's engine is made first all the same:
    a session whose first test made no engine leaves the later tests' engine
    on another loop."""


def _q(kind="single", seconds=20, **st):
    options = [{"key": k, "label": k.upper()} for k in "abcd"]
    q = {
        "id": "q1",
        "kind": kind,
        "seconds": seconds,
        "options": options,
        "correct_key": "a",
        "settings": st,
        "points": st.pop("points", None),
        "double": st.pop("double", False),
    }
    if kind == "type":
        q["options"] = []
    return q


def _sc(**change):
    return {**DEFAULT, **change}


def _score(q, response, elapsed, sc=None, streak_before=0, continues=False, total=0):
    return scoring.score_answer(
        question=q, response=response, scoring=sc or DEFAULT, elapsed=elapsed, streak_before=streak_before, streak_continues=continues, total_before=total
    )


# ─── Today's default, bit for bit ─────────────────────────────────────────


async def test_the_default_reproduces_the_rooms_points_for_every_time_bit_for_bit():
    rnd = random.Random(7)
    times = [0, 0.001, 5, 10, 13.7, 19.999, 20, 21.3, 22.0] + [rnd.uniform(0, 22) for _ in range(5000)]
    for seconds in (5, 10, 20, 30, 60, 120):
        for e in times:
            e = e * seconds / 20
            want = scoring.legacy_points(True, e, seconds)
            got = _score(_q(seconds=seconds), {"option_key": "a"}, e).points
            assert got == want, (seconds, e, got, want)
            assert _score(_q(seconds=seconds), {"option_key": "b"}, e).points == scoring.legacy_points(False, e, seconds) == 0


@pytest.mark.parametrize(("elapsed", "points"), [(0, 1000), (5, 875), (13.7, 657), (20.0, 500), (21.3, 500)])
async def test_the_contracts_worked_examples_for_the_default(elapsed, points):
    assert _score(_q(), {"option_key": "a"}, elapsed).points == points


async def test_zero_time_and_a_clock_behind_give_full_points_never_more():
    assert _score(_q(), {"option_key": "a"}, 0).points == 1000
    assert _score(_q(), {"option_key": "a"}, -3.5).points == 1000  # a clock a little behind is clamped at zero
    assert scoring.clamp_elapsed(-1, 20) == 0 and scoring.clamp_elapsed(99, 20) == 20


async def test_a_late_answer_in_the_grace_counts_as_the_clock_running_out():
    s = _score(_q(), {"option_key": "a"}, 21.9)
    assert s.points == 500 and s.saved_ms == 0 and s.elapsed_ms == 20000


# ─── Speed: off, linear, steps ───────────────────────────────────────────


async def test_speed_off_gives_the_question_points_whenever():
    for e in (0, 7, 20, 21.5):
        assert _score(_q(), {"option_key": "a"}, e, _sc(speed="off")).points == 1000


async def test_linear_with_another_share():
    assert _score(_q(), {"option_key": "a"}, 5, _sc(speed_share=0.8)).points == 800
    assert _score(_q(), {"option_key": "a"}, 0, _sc(speed_share=0.0)).points == 1000
    assert _score(_q(), {"option_key": "a"}, 20, _sc(speed_share=1.0)).points == 0  # all on speed, none left at the clock


@pytest.mark.parametrize(("elapsed", "points"), [(0, 1000), (4, 1000), (5, 1000), (7, 800), (10, 800), (15, 600), (16, 400), (21, 400)])
async def test_steps_pay_by_the_first_tier_not_passed(elapsed, points):
    assert _score(_q(), {"option_key": "a"}, elapsed, _sc(speed="steps")).points == points


async def test_a_single_step_is_flat():
    assert _score(_q(), {"option_key": "a"}, 19, _sc(speed="steps", steps=[{"until": 1.0, "share": 0.7}])).points == 700


async def test_points_override_and_double_points():
    assert scoring.question_points(None, 1000, False) == 1000
    assert scoring.question_points(250, 1000, True) == 500
    assert _score(_q(double=True), {"option_key": "a"}, 5).points == 1750
    assert _score(_q(points=2000), {"option_key": "a"}, 0).points == 2000
    assert _score(_q(points=0), {"option_key": "a"}, 0).points == 0
    assert _score(_q(), {"option_key": "a"}, 0, _sc(points=0)).points == 0


# ─── Wrong answers and the floor ─────────────────────────────────────────


async def test_negative_marking_takes_a_share_of_the_questions_points():
    assert _score(_q(), {"option_key": "b"}, 3, _sc(wrong_penalty=0.25), total=5000).points == -250
    assert _score(_q(double=True), {"option_key": "b"}, 3, _sc(wrong_penalty=0.5), total=5000).points == -1000
    assert _score(_q(points=1001), {"option_key": "b"}, 3, _sc(wrong_penalty=0.25), total=5000).points == -250  # truncated


async def test_the_floor_keeps_a_total_at_zero_and_records_what_was_taken():
    s = _score(_q(), {"option_key": "b"}, 3, _sc(wrong_penalty=0.25), total=100)
    assert s.points == -100 and s.earned == -250
    assert _score(_q(), {"option_key": "b"}, 3, _sc(wrong_penalty=0.25), total=0).points == 0
    allowed = _score(_q(), {"option_key": "b"}, 3, _sc(wrong_penalty=0.25, allow_negative=True), total=100)
    assert allowed.points == -250


async def test_partial_credit_is_never_penalised_and_no_penalty_by_default():
    q = _q("multi", correct_keys=["a", "c"], multi_rule="proportional")
    assert _score(q, {"option_keys": ["a"]}, 5, _sc(wrong_penalty=0.5)).points > 0
    assert _score(_q(), {"option_key": "b"}, 3).points == 0


# ─── Kinds: credit ───────────────────────────────────────────────────────


async def test_multi_rules():
    k = ["a", "c"]
    assert scoring.multi_credit(["a", "c"], k, "all") == 1 and scoring.multi_credit(["a"], k, "all") == 0
    assert scoring.multi_credit(["a"], k, "proportional") == 0.5
    assert scoring.multi_credit(["a", "b"], k, "proportional") == 0
    assert scoring.multi_credit(["a"], k, "subtract") == 0.5
    assert scoring.multi_credit(["a", "b"], k, "subtract") == 0
    assert scoring.multi_credit(["a", "b", "c"], k, "subtract") == 0.5
    assert scoring.multi_credit(["b", "d"], k, "subtract") == 0  # never below zero
    assert scoring.multi_credit(["c", "a"], k, "proportional") == 1


async def test_multi_partial_points_match_the_worked_example():
    q = _q("multi", correct_keys=["a", "c"], multi_rule="proportional")
    s = _score(q, {"option_keys": ["a"]}, 5)
    assert s.points == 437 and s.credit == 0.5 and s.correct is False
    full = _score(q, {"option_keys": ["c", "a"]}, 5)
    assert full.points == 875 and full.correct is True


async def test_typed_answers_exact_and_close():
    acc = ["São Paulo"]
    assert scoring.text_matches("sao  paulo", acc, "exact")
    assert scoring.text_matches("  SÃO PAULO ", acc, "exact")
    assert not scoring.text_matches("Sao Polo", acc, "exact")
    assert scoring.text_matches("Sao Polo", acc, "close")
    assert not scoring.text_matches("Rio", acc, "close")
    assert not scoring.text_matches("cats", ["cat"], "close")  # three letters: no typo forgiven
    assert scoring.text_matches("Pariss", ["Paris"], "close") and not scoring.text_matches("Parisss", ["Paris"], "close")
    assert scoring.text_matches("Bengaluru", ["Bangalore", "Bengaluru"], "exact")
    assert not scoring.text_matches("C", ["C++"], "close")  # punctuation is kept
    assert scoring.text_matches("teh", ["the"], "close") is False  # three letters
    assert scoring.text_matches("recieve", ["receive"], "close")  # a swap is one
    assert not scoring.text_matches("", ["x"], "close")


async def test_edit_distance_and_tolerance():
    assert scoring.edit_distance("abcd", "abdc", 2) == 1
    assert scoring.edit_distance("kitten", "sitting", 5) == 3
    assert scoring.edit_distance("abc", "abcdefgh", 2) == 3  # cut off past the limit
    assert [scoring.tolerance(n) for n in (1, 3, 4, 7, 8, 30)] == [0, 0, 1, 1, 2, 2]


async def test_order_rules():
    right = ["a", "b", "c", "d"]
    assert scoring.order_credit(right, right, "all") == 1
    assert scoring.order_credit(["a", "c", "b", "d"], right, "all") == 0
    assert scoring.order_credit(["a", "c", "b", "d"], right, "partial") == 0.5
    assert scoring.order_credit(["d", "c", "b", "a"], right, "partial") == 0
    q = _q("order", order_rule="partial")
    s = _score(q, {"order": ["a", "c", "b", "d"]}, 0)
    assert s.points == 500 and s.correct is False


async def test_true_false_and_a_poll_question():
    tf = _q("true_false")
    assert _score(tf, {"option_key": "a"}, 0).points == 1000
    poll = _q("poll")
    s = _score(poll, {"option_key": "b"}, 0, _sc(wrong_penalty=0.5, streak_from=2), streak_before=4, continues=True)
    assert s.points == 0 and s.credit is None and s.streak == 4 and s.bonus == 0  # no score, the streak untouched


# ─── Streaks ─────────────────────────────────────────────────────────────


async def test_a_streak_pays_on_the_nth_right_answer_in_a_row_and_after():
    sc = _sc(streak_from=3, streak_bonus=100)
    q = _q()
    streak, bonuses = 0, []
    for right in [True, True, True, True, False, True, True, True]:
        s = _score(q, {"option_key": "a" if right else "b"}, 0, sc, streak_before=streak, continues=streak > 0)
        streak = s.streak
        bonuses.append(s.bonus)
    assert bonuses == [0, 0, 100, 100, 0, 0, 0, 100]


async def test_a_streak_resets_on_a_partial_answer_and_when_a_question_was_missed():
    sc = _sc(streak_from=2, streak_bonus=50)
    multi = _q("multi", correct_keys=["a", "c"], multi_rule="proportional")
    assert _score(multi, {"option_keys": ["a"]}, 0, sc, streak_before=5, continues=True).streak == 0
    missed = _score(_q(), {"option_key": "a"}, 0, sc, streak_before=5, continues=False)  # the last right answer was not the previous question
    assert missed.streak == 1 and missed.bonus == 0
    assert _score(_q(), {"option_key": "a"}, 0, sc, streak_before=1, continues=True).bonus == 50
    off = _score(_q(), {"option_key": "a"}, 0, _sc(streak_from=0), streak_before=9, continues=True)
    assert off.bonus == 0


async def test_a_streak_bonus_is_added_before_the_floor_and_counts_with_double_points():
    s = _score(_q(double=True), {"option_key": "a"}, 0, _sc(streak_from=2, streak_bonus=100), streak_before=1, continues=True)
    assert s.points == 2100


# ─── Saved time and ties ─────────────────────────────────────────────────


async def test_saved_time_is_what_was_left_on_the_clock():
    assert _score(_q(), {"option_key": "a"}, 5.5).saved_ms == 14500
    assert _score(_q(), {"option_key": "b"}, 0).saved_ms == 20000
    assert _score(_q(), {"option_key": "a"}, 25).saved_ms == 0


async def test_team_boards_total_and_average():
    teams = [{"key": "red", "name": "Red"}, {"key": "blue", "name": "Blue"}, {"key": "green", "name": "Green"}]
    rows = [("red", 3, 3000), ("blue", 1, 1500), (None, 4, 999)]
    total = scoring.team_board(teams, rows, "total")
    assert [(t["key"], t["score"], t["rank"]) for t in total] == [("red", 3000, 1), ("blue", 1500, 2), ("green", 0, 3)]
    avg = scoring.team_board(teams, [("red", 3, 3001), ("blue", 2, 3001)], "average")
    assert [(t["key"], t["score"]) for t in avg] == [("blue", 1501), ("red", 1000), ("green", 0)]
    tied = scoring.team_board(teams[:2], [("red", 1, 10), ("blue", 1, 10)], "total")
    assert [t["rank"] for t in tied] == [1, 1]


async def test_auto_teams_fill_the_smallest_first():
    teams = [{"key": "a"}, {"key": "b"}, {"key": "c"}]
    assert scoring.pick_team(teams, {}) == "a"
    assert scoring.pick_team(teams, {"a": 2, "b": 1, "c": 1}) == "b"
    assert scoring.pick_team([], {}) is None


async def test_order_questions_are_shown_shuffled_the_same_everywhere_never_in_order():
    opts = [{"key": k, "label": k} for k in "abcd"]
    for seed in range(200):
        shown = scoring.shuffled(opts, f"q{seed}")
        assert sorted(o["key"] for o in shown) == list("abcd") and [o["key"] for o in shown] != list("abcd")
        assert shown == scoring.shuffled(opts, f"q{seed}")
    assert [o["key"] for o in scoring.shuffled(opts[:2], "x")] == ["b", "a"]


async def test_the_tag_is_three_characters_of_the_id():
    assert scoring.tag_of("6f1c2a9e-0000-4000-8000-0000000007f2") == "7F2"


# ─── Settings: defaults, merge, validation ───────────────────────────────


async def test_settings_resolve_to_todays_rules_and_merge_key_by_key():
    st = live_settings.quiz_settings(None)
    assert st["scoring"]["points"] == 1000 and st["scoring"]["speed"] == "linear" and st["scoring"]["speed_share"] == 0.5
    assert st["flow"] == {"advance": "host", "auto_seconds": 8, "reveal": "time_up", "board": "every", "board_every": 3}
    assert st["ties"] == "join" and st["teams"]["mode"] == "off"
    saved = live_settings.saved_change(None, {"scoring": {"wrong_penalty": 0.25}})
    saved = live_settings.saved_change(saved, {"scoring": {"streak_from": 3}, "ties": "time"})
    st = live_settings.quiz_settings(saved)
    assert st["scoring"]["wrong_penalty"] == 0.25 and st["scoring"]["streak_from"] == 3 and st["ties"] == "time" and st["scoring"]["points"] == 1000
    assert live_settings.saved_change(None, None) is None
    assert live_settings.scores_can_fall(st) and not live_settings.scores_can_fall(live_settings.quiz_settings(None))


@pytest.mark.parametrize(
    "change",
    [
        {"scoring": {"points": 10001}},
        {"scoring": {"points": 1.5}},
        {"scoring": {"speed": "fast"}},
        {"scoring": {"speed_share": 1.2}},
        {"scoring": {"wrong_penalty": 0.3}},
        {"scoring": {"streak_from": 1}},
        {"scoring": {"streak_from": 21}},
        {"scoring": {"allow_negative": "yes"}},
        {"scoring": {"steps": []}},
        {"scoring": {"steps": [{"until": 0.5, "share": 1}]}},
        {"scoring": {"steps": [{"until": 0.5, "share": 1}, {"until": 0.4, "share": 1}, {"until": 1.0, "share": 1}]}},
        {"flow": {"advance": "sometimes"}},
        {"flow": {"auto_seconds": 2}},
        {"flow": {"board_every": 1}},
        {"players": {"names": "any"}},
        {"teams": {"mode": "pick", "list": [{"key": "red", "name": "Red", "color": "#E11D48"}]}},
        {"teams": {"mode": "pick", "list": [{"key": "Red Team", "name": "Red", "color": "#E11D48"}, {"key": "b", "name": "B", "color": "#000000"}]}},
        {"teams": {"mode": "pick", "list": [{"key": "r", "name": "R", "color": "red"}, {"key": "b", "name": "B", "color": "#000000"}]}},
        {"ties": "random"},
        {"unknown": 1},
    ],
)
async def test_settings_out_of_range_are_refused(change):
    with pytest.raises(CustomHTTPException) as e:
        live_settings.saved_change(None, change)
    assert e.value.status_code == 422 and e.value.errors["field"]


async def test_teams_are_cleaned():
    saved = live_settings.saved_change(
        None,
        {
            "teams": {
                "mode": "auto",
                "score": "average",
                "list": [{"key": "RED", "name": "  Red   Rockets ", "color": "#e11d48"}, {"key": "b", "name": "Blue", "color": "#2563eb"}],
            }
        },
    )
    cleaned = live_settings.clean_quiz_settings(live_settings.quiz_settings(saved))
    assert cleaned["teams"]["list"][0] == {"key": "red", "name": "Red Rockets", "color": "#E11D48"}


async def test_questions_are_checked_by_kind():
    q = live_settings.clean_question({"prompt": "Which?", "options": [{"key": "a", "label": "x"}, {"key": "b", "label": "y"}], "correct_key": "a"})
    assert q["kind"] is None and q["settings"] is None and q["correct_key"] == "a"  # today's question, stored as before
    tf = live_settings.clean_question({"kind": "true_false", "prompt": "Sky is blue", "correct_key": "a"})
    assert [o["label"] for o in tf["options"]] == ["True", "False"]
    m = live_settings.clean_question(
        {
            "kind": "multi",
            "prompt": "Primes?",
            "options": [{"key": k, "label": k} for k in "abcd"],
            "correct_keys": ["c", "a"],
            "multi_rule": "subtract",
            "points": 2000,
            "double": True,
        }
    )
    assert m["settings"] == {"points": 2000, "double": True, "correct_keys": ["a", "c"], "multi_rule": "subtract"} and m["correct_key"] == "a"
    t = live_settings.clean_question({"kind": "type", "prompt": "Capital?", "accepted": ["Delhi", " New  Delhi ", "Delhi"], "match": "close"})
    assert t["options"] == [] and t["settings"]["accepted"] == ["Delhi", "New Delhi"] and t["correct_key"] == ""
    o = live_settings.clean_question({"kind": "order", "prompt": "Oldest first", "options": [{"key": k, "label": k} for k in "abc"]})
    assert o["settings"] == {"order_rule": "all"}
    for bad in (
        {"kind": "multi", "prompt": "Primes?", "options": [{"key": "a", "label": "x"}, {"key": "b", "label": "y"}], "correct_keys": []},
        {"kind": "type", "prompt": "Capital?", "accepted": []},
        {"kind": "type", "prompt": "Capital?", "accepted": ["x"], "options": [{"key": "a", "label": "x"}]},
        {"kind": "true_false", "prompt": "Sky", "correct_key": "z"},
        {"kind": "single", "prompt": "Which?", "options": [{"key": "a", "label": "x"}], "correct_key": "a"},
        {"kind": "single", "prompt": "Which?", "options": [{"key": "a", "label": "x"}, {"key": "a", "label": "y"}], "correct_key": "a"},
        {"kind": "order", "prompt": "Order", "options": [{"key": k, "label": k} for k in "abcdefg"]},
        {"kind": "nope", "prompt": "Which?"},
        {"prompt": "Which?", "options": [{"key": "a", "label": "x"}, {"key": "b", "label": "y"}], "correct_key": "a", "seconds": 301},
    ):
        with pytest.raises(CustomHTTPException):
            live_settings.clean_question(bad)


async def test_answers_are_checked_by_kind():
    q = {"kind": "multi", "options": [{"key": k, "label": k} for k in "abc"]}
    assert live_settings.check_response(q, {"option_keys": ["c", "a"]}) == {"option_keys": ["a", "c"]}
    order = {"kind": "order", "options": [{"key": k, "label": k} for k in "abc"]}
    assert live_settings.check_response(order, {"order": ["c", "a", "b"]}) == {"order": ["c", "a", "b"]}
    typed = {"kind": "type", "options": []}
    assert live_settings.check_response(typed, {"text": "  New   Delhi "}) == {"text": "New Delhi"}
    for question, body in (
        (q, {"option_keys": ["a", "a"]}),
        (q, {"option_keys": []}),
        (q, {"option_key": "a"}),
        (q, {"option_keys": ["z"]}),
        (order, {"order": ["a", "b"]}),
        (order, {"order": ["a", "a", "b"]}),
        (typed, {"text": ""}),
        (typed, {"text": "x" * 121}),
        (typed, {"option_key": "a"}),
        ({"kind": "single", "options": [{"key": "a", "label": "x"}]}, {"option_key": "b"}),
    ):
        with pytest.raises(CustomHTTPException) as e:
            live_settings.check_response(question, body)
        assert e.value.status_code == 422


async def test_poll_settings_defaults_and_checks():
    st = live_settings.poll_settings("choice", None)
    assert st["max_choices"] == 1 and st["results"] == "live" and st["change_vote"] is True and st["chart"] == "bar"
    assert live_settings.poll_settings("words", None)["chart"] == "cloud"
    assert live_settings.poll_settings("rating", {"scale": 10})["style"] == "numbers"
    assert live_settings.poll_settings_saved("choice", ["a", "b", "c"], None, {"max_choices": 2}) == {"max_choices": 2}
    for kind, opts, change in (
        ("choice", ["a", "b"], {"max_choices": 3}),
        ("rating", [], {"scale": 7}),
        ("choice", ["a", "b"], {"chart": "cloud"}),
        ("choice", ["a", "b"], {"seconds": 5}),
        ("choice", ["a", "b"], {"hide_results": "no"}),
        ("choice", ["a"], {"option_images": ["x", "y"]}),
        ("choice", ["a", "b"], {"what": 1}),
        ("words", [], {"max_choices": 2}),
    ):
        with pytest.raises(CustomHTTPException):
            live_settings.poll_settings_saved(kind, opts, None, change)
    assert live_settings.poll_needs_update("ranking", {}) and live_settings.poll_needs_update("rating", {"scale": 10})
    assert not live_settings.poll_needs_update("rating", {"scale": 5}) and not live_settings.poll_needs_update("choice", {"max_choices": 3})
    assert live_settings.poll_results_hidden({"results": "closed"}, "live") and not live_settings.poll_results_hidden({"results": "closed"}, "closed")


# ─── The theme ───────────────────────────────────────────────────────────


async def test_contrast_is_wcags():
    assert round(theme.contrast("#FFFFFF", "#000000"), 2) == 21.0
    assert round(theme.contrast("#777777", "#FFFFFF"), 2) == 4.48
    assert theme.contrast("#123456", "#123456") == 1.0


async def test_text_on_every_fill_reads_at_aa():
    rnd = random.Random(3)
    for _ in range(3000):
        c = "#%06X" % rnd.randrange(0x1000000)
        t = theme.text_on(c)
        assert t is not None and theme.contrast(t, c) >= 4.5, c
    assert theme.text_on("#FFFFFF") == theme.DARK and theme.text_on("#0B0E0D") == theme.LIGHT
    assert theme.text_on("#FFFFFF", "#000000") is None  # nothing reads on both ends


async def test_every_preset_is_valid_and_readable():
    class Club:
        brand_primary, brand_secondary = "#F2AA4C", "#101820"

    for key in theme.PRESETS:
        saved, verdict = theme.clean({"preset": key}, Club(), None)
        assert not verdict.errors, (key, verdict.errors)
        resolved = theme.resolve(saved, {}, None)
        assert theme.contrast(resolved["text"]["on_background"], resolved["colors"]["background"]) >= 4.5
    club, _ = theme.clean({"preset": "club"}, Club(), None)
    assert club["colors"]["accent"] == "#F2AA4C" and club["colors"]["answers"][0] == "#101820"

    class Pale:
        brand_primary, brand_secondary = "#101010", None

    pale, _ = theme.clean({"preset": "club"}, Pale(), None)
    assert pale["colors"]["background"] == theme.LIGHT_BG  # a dark brand colour goes on the light background


async def test_the_contrast_rules_block_and_warn():
    _, v = theme.clean({"preset": "meridian_dark", "colors": {"accent": "#1A1F1E"}})
    assert v.errors and v.errors[0]["field"] == "colors.accent" and v.errors[0]["ratio"] < 3
    _, v = theme.clean({"preset": "meridian_dark", "colors": {"answers": ["#DC2626", "#DC2626", "#D97706", "#16A34A", "#7C3AED", "#0891B2"]}})
    assert v.errors[0]["field"] == "colors.answers"
    _, v = theme.clean({"preset": "meridian_dark", "colors": {"answers": ["#0C100F", "#2563EB", "#D97706", "#16A34A", "#7C3AED", "#0891B2"]}})
    assert not v.errors and v.warnings[0]["code"] == "ANSWER_BLENDS"
    _, v = theme.clean({"preset": "meridian_dark", "background": {"kind": "gradient", "gradient": {"from": "#FFFFFF", "to": "#000000", "angle": 90}}})
    assert v.errors and v.errors[0]["field"] == "background.gradient"


@pytest.mark.parametrize(
    "body",
    [
        {"preset": "neon"},
        {"colors": {"background": "red"}},
        {"colors": {"answers": ["#FFFFFF"]}},
        {"font": {"display": "comic_sans"}},
        {"timer": "sand"},
        {"screen": {"text_scale": 2.0}},
        {"guest": {"text_scale": 0.5}},
        {"background": {"kind": "image"}},
        {"background": {"kind": "gradient"}},
        {"leaderboard": {"size": 7}},
        {"screen": {"join": "middle"}},
        {"answers": {"style": "emoji"}},
        {"css": "body{}"},
        {"screen": {"font": "x"}},
        {"background": {"kind": "color", "dim": 0.1}},
    ],
)
async def test_theme_values_outside_the_table_are_refused(body):
    with pytest.raises(CustomHTTPException) as e:
        theme.clean({"preset": "meridian_dark", **body})
    assert e.value.status_code == 422


async def test_a_left_out_field_takes_the_presets_value():
    saved, _ = theme.clean({"preset": "high_contrast", "timer": "ring"})
    assert saved["timer"] == "ring" and saved["colors"]["background"] == "#000000" and saved["screen"]["text_scale"] == 1.15


async def test_resolving_drops_missing_pictures_and_versions_change():
    saved, _ = theme.clean(
        {"preset": "meridian_dark", "logo": "6f1c2a9e00004000800000000000000a", "background": {"kind": "image", "image": "6f1c2a9e00004000800000000000000b"}}
    )
    gone = theme.resolve(saved, {}, None)
    assert gone["logo"] is None and gone["background"]["kind"] == "color"
    pic = {"id": "x", "large": "https://cdn/x.jpg"}
    shown = theme.resolve(saved, {"6f1c2a9e00004000800000000000000a": pic, "6f1c2a9e00004000800000000000000b": pic}, None)
    assert shown["logo"] == pic and shown["background"]["kind"] == "image"
    assert theme.version(gone, True) != theme.version(shown, True) and theme.version(gone, True) != theme.version(gone, False)


# ─── Names ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", ["fuck", "F u c k", "sh1t", "BITCHHH", "s.h.i.t", "xfuckerx", "Madarchod", "Shiiiit"])
async def test_the_name_filter_refuses(name):
    assert names.refused(name)


@pytest.mark.parametrize("name", ["Sneha J", "Rohan", "Dickens", "Scunthorpe", "Cassie", "Essex Lad", "Anu", "Shital", "Arsenal fan", "Hancock"])
async def test_the_name_filter_lets_ordinary_names_through(name):
    assert not names.refused(name)
