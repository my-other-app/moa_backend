"""The quiz's scoring, as pure functions: no database, no clock.

What one answer is worth is decided here, from the round's settings
(app/api/live/settings.py), the question and the answer, and nothing else.
docs: feature-register/live-custom/CONTRACT.md, section 4, has the formulas
and worked examples; the tests (tests/test_live_scoring.py) check every one.

Notation:

- P: the question's points (its own, or the round's), doubled for a
  double-points question.
- T: the question's seconds; e: seconds from its opening to the answer,
  clamped to [0, T] (an answer in the grace after the clock counts as T).
- f = 1 - e/T, the speed left; u = e/T, the time used.
- c: the answer's credit, 0..1, from the question's kind.

With nothing set (P = 1000, linear speed with half the points on speed) a
right answer is int(500.0 + 500.0 * f): the same floating-point expression
the room has always used, so every score is the same as before.
"""
from __future__ import annotations

import math
import random
import unicodedata
from dataclasses import dataclass
from typing import Optional, Sequence

#: The kinds of question. `single` is the one there always was.
SINGLE, MULTI, TRUE_FALSE, TYPE, ORDER, POLL = "single", "multi", "true_false", "type", "order", "poll"
KINDS = (SINGLE, MULTI, TRUE_FALSE, TYPE, ORDER, POLL)
#: The kinds answered with one option key (and so playable by a client from before the kinds).
ONE_KEY = (SINGLE, TRUE_FALSE, POLL)
#: The kinds a client from before the kinds is told to update for.
NEEDS_UPDATE = (MULTI, TYPE, ORDER)


# ─── Speed ────────────────────────────────────────────────────────────────


def clamp_elapsed(elapsed: float, seconds: int) -> float:
    """e: never below 0 (a clock a little behind), never past T (the grace)."""
    if seconds <= 0:
        return 0.0
    return min(max(float(elapsed), 0.0), float(seconds))


def step_share(steps: Sequence[dict], used: float) -> float:
    """The share of the first step whose `until` the time used has not passed."""
    for s in steps:
        if used <= float(s["until"]):
            return float(s["share"])
    return float(steps[-1]["share"]) if steps else 1.0


def raw_points(points: int, credit: float, elapsed: float, seconds: int, scoring: dict) -> int:
    """What a right or partly right answer is worth before its streak bonus
    (credit > 0). Speed is `off`, `linear` or `steps`."""
    if credit <= 0 or points <= 0:
        return 0
    e = clamp_elapsed(elapsed, seconds)
    speed = scoring.get("speed", "linear")
    pc = points * credit
    if speed == "off":
        return int(pc)
    if speed == "steps":
        used = e / seconds if seconds > 0 else 0.0
        return int(pc * step_share(scoring.get("steps") or [], used))
    s = float(scoring.get("speed_share", 0.5))
    f = 1 - min(e, seconds) / seconds if seconds > 0 else 1.0
    # As two terms, as the room always added them: 1000 * 1.0 * 0.5 is 500.0
    # exactly, so the default is int(500.0 + 500.0 * f) to the last bit.
    return int(pc * (1 - s) + pc * s * f)


def penalty_points(points: int, penalty: float) -> int:
    """What a wrong answer loses (as a negative number, or 0)."""
    if penalty <= 0 or points <= 0:
        return 0
    return -int(points * penalty)


def question_points(question_points_: Optional[int], round_points: int, double: bool) -> int:
    p = int(round_points if question_points_ is None else question_points_)
    return p * 2 if double else p


# ─── Credit, per kind ─────────────────────────────────────────────────────


def normalise(text: str) -> str:
    """Case, accents and spacing ignored; punctuation kept ("C++" is not "C")."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    bare = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(bare.casefold().split())


def edit_distance(a: str, b: str, limit: int) -> int:
    """Optimal string alignment distance (insert, delete, substitute, swap two
    neighbours: 1 each), cut off past `limit` (anything more is limit + 1)."""
    if a == b:
        return 0
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev2: Optional[list[int]] = None
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        best = cur[0]
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            v = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if prev2 is not None and i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                v = min(v, prev2[j - 2] + 1)
            cur[j] = v
            best = min(best, v)
        if best > limit:
            return limit + 1
        prev2, prev = prev, cur
    return prev[len(b)]


def tolerance(length: int) -> int:
    """Typos a close match forgives: none up to 3 characters, 1 up to 7, then 2."""
    if length <= 3:
        return 0
    if length <= 7:
        return 1
    return 2


def text_matches(text: str, accepted: Sequence[str], match: str = "exact") -> bool:
    given = normalise(text)
    if not given:
        return False
    for a in accepted:
        want = normalise(a)
        if not want:
            continue
        if given == want:
            return True
        if match == "close":
            tol = tolerance(len(want))
            if tol and edit_distance(given, want, tol) <= tol:
                return True
    return False


def multi_credit(chosen: Sequence[str], correct: Sequence[str], rule: str = "all") -> float:
    c, k = set(chosen), set(correct)
    if not k:
        return 0.0
    if rule == "all":
        return 1.0 if c == k else 0.0
    right, wrong = len(c & k), len(c - k)
    if rule == "proportional":
        return right / len(k) if wrong == 0 else 0.0
    # subtract: each wrong pick takes back one right one.
    return max(0.0, (right - wrong) / len(k))


def order_credit(given: Sequence[str], correct: Sequence[str], rule: str = "all") -> float:
    if not correct:
        return 0.0
    if rule == "all":
        return 1.0 if list(given) == list(correct) else 0.0
    placed = sum(1 for g, c in zip(given, correct) if g == c)
    return placed / len(correct)


@dataclass(frozen=True)
class Judged:
    """An answer judged: its credit (None for a poll question) and whether it was wholly right."""

    credit: Optional[float]
    correct: bool


def judge(question: dict, response: dict) -> Judged:
    """The credit for `response` (already checked for shape) on `question`
    (the room's dict: kind, correct_key, settings)."""
    kind = question.get("kind") or SINGLE
    st = question.get("settings") or {}
    if kind == POLL:
        return Judged(None, False)
    if kind in (SINGLE, TRUE_FALSE):
        ok = response.get("option_key") == question.get("correct_key")
        return Judged(1.0 if ok else 0.0, ok)
    if kind == MULTI:
        correct = st.get("correct_keys") or []
        chosen = response.get("option_keys") or []
        credit = multi_credit(chosen, correct, st.get("multi_rule") or "all")
        return Judged(credit, set(chosen) == set(correct))
    if kind == TYPE:
        ok = text_matches(response.get("text") or "", st.get("accepted") or [], st.get("match") or "exact")
        return Judged(1.0 if ok else 0.0, ok)
    if kind == ORDER:
        correct = [o["key"] for o in question.get("options") or []]
        given = response.get("order") or []
        credit = order_credit(given, correct, st.get("order_rule") or "all")
        return Judged(credit, list(given) == correct)
    return Judged(0.0, False)


# ─── One answer, scored ─────────────────────────────────────────────────


@dataclass(frozen=True)
class Scored:
    #: What the answer adds to the total, after the floor (what my_points shows).
    points: int
    #: Before the floor: what the rules give (the floor only ever raises a loss).
    earned: int
    credit: Optional[float]
    correct: bool
    #: The player's streak after this answer.
    streak: int
    bonus: int
    saved_ms: int
    elapsed_ms: int


def score_answer(
    *, question: dict, response: dict, scoring: dict, elapsed: float, streak_before: int, streak_continues: bool, total_before: int,
) -> Scored:
    """Everything one answer does to the player's score.

    `streak_before` is the player's streak, and `streak_continues` whether
    their last fully right answer was on the question asked just before this
    one (so a right answer now extends it rather than starting a new one).
    `total_before` is their score before this answer, for the floor."""
    seconds = int(question.get("seconds") or 20)
    e = clamp_elapsed(elapsed, seconds)
    elapsed_ms = int(e * 1000)
    saved_ms = max(0, int((seconds - e) * 1000))
    judged = judge(question, response)
    if judged.credit is None:  # a poll question: no points, the streak untouched
        return Scored(0, 0, None, False, streak_before, 0, saved_ms, elapsed_ms)
    p = question_points(question.get("points"), int(scoring.get("points", 1000)), bool(question.get("double")))
    if judged.credit > 0:
        earned = raw_points(p, judged.credit, elapsed, seconds, scoring)
    else:
        earned = penalty_points(p, float(scoring.get("wrong_penalty") or 0))
    streak = (streak_before + 1 if streak_continues else 1) if judged.correct else 0
    bonus = 0
    n = int(scoring.get("streak_from") or 0)
    if n >= 2 and judged.correct and streak >= n:
        bonus = int(scoring.get("streak_bonus") or 0)
    earned += bonus
    points = earned
    if not scoring.get("allow_negative") and total_before + points < 0:
        points = -max(0, total_before)
    return Scored(points, earned, judged.credit, judged.correct, streak, bonus, saved_ms, elapsed_ms)


def legacy_points(correct: bool, elapsed: float, seconds: int) -> int:
    """The room's points before the settings, for the tests' proof that the
    defaults give the same: half for being right, half for being quick."""
    return int(500 + 500 * max(0.0, 1 - min(elapsed, seconds) / seconds)) if correct else 0


# ─── What players are shown ───────────────────────────────────────────────


def shuffled(options: Sequence[dict], seed: str) -> list[dict]:
    """An order question's options as the players see them: the right order
    shuffled, the same on every phone (seeded by the question). Never the
    right order itself when there are two or more."""
    out = list(options)
    if len(out) < 2:
        return out
    rnd = random.Random(seed)
    for _ in range(8):
        rnd.shuffle(out)
        if [o["key"] for o in out] != [o["key"] for o in options]:
            return out
    return out[1:] + out[:1]


def tag_of(player_id) -> str:
    """Three hex characters, the player's name on an anonymous big screen."""
    return str(player_id).replace("-", "")[-3:].upper()


# ─── Teams and ranks ──────────────────────────────────────────────────────


def team_board(teams: Sequence[dict], rows: Sequence[tuple], how: str = "total") -> list[dict]:
    """The teams' board: `rows` are (team key, players, sum of scores).
    Average is the sum over the players, rounded half up; an empty team 0."""
    by = {k: (int(n or 0), int(s or 0)) for k, n, s in rows}
    out = []
    for i, t in enumerate(teams):
        n, s = by.get(t["key"], (0, 0))
        score = s if how == "total" else (math.floor(s / n + 0.5) if n else 0)
        out.append({"key": t["key"], "name": t["name"], "color": t.get("color"), "players": n, "score": score, "_order": i})
    out.sort(key=lambda r: (-r["score"], r["_order"]))
    rank, last = 0, None
    for i, r in enumerate(out):
        if r["score"] != last:
            rank, last = i + 1, r["score"]
        r["rank"] = rank
        r.pop("_order")
    return out


def pick_team(teams: Sequence[dict], counts: dict) -> Optional[str]:
    """Auto mode: the team with the fewest players, ties to the first listed."""
    if not teams:
        return None
    return min(teams, key=lambda t: (int(counts.get(t["key"], 0)), teams.index(t)))["key"]
