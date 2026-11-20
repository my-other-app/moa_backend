"""A round's settings, a question's kind and a poll's settings: their
defaults, how a change is merged in, and what is allowed.

Everything is stored as the organiser left it (a JSON column, null when
never set) and resolved on read: the defaults first, what was saved on top.
The defaults are the room as it has always been, so a quiz or a poll nobody
edited behaves exactly as before. CONTRACT.md sections 3 and 7 are the
reference for every field here.
"""

from __future__ import annotations

import copy
import re
from typing import Optional

from app.api.live import scoring
from app.response import CustomHTTPException

# ─── A round's settings ───────────────────────────────────────────────────

DEFAULT_STEPS = [{"until": 0.25, "share": 1.0}, {"until": 0.5, "share": 0.8}, {"until": 0.75, "share": 0.6}, {"until": 1.0, "share": 0.4}]

QUIZ_DEFAULTS: dict = {
    "scoring": {
        "points": 1000,
        "speed": "linear",
        "speed_share": 0.5,
        "steps": DEFAULT_STEPS,
        "wrong_penalty": 0,
        "allow_negative": False,
        "streak_from": 0,
        "streak_bonus": 100,
    },
    "flow": {"advance": "host", "auto_seconds": 8, "reveal": "time_up", "board": "every", "board_every": 3},
    "players": {"names": "nickname", "filter_names": False, "late_join": True, "one_per_ticket": False, "anonymous_screen": False},
    "teams": {"mode": "off", "score": "total", "list": []},
    "ties": "join",
    "overall": True,
}

_TEAM_KEY = re.compile(r"^[a-z0-9_-]{1,16}$")
_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")


def _bad(field: str, message: str, code: str = "QUIZ_SETTINGS") -> CustomHTTPException:
    return CustomHTTPException(422, message=message, error_code=code, errors={"field": field})


def merge(base: dict, change: Optional[dict]) -> dict:
    """`change` onto `base`: each sub-object key by key, everything else replaced."""
    out = copy.deepcopy(base)
    for k, v in (change or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = {**out[k], **v}
        else:
            out[k] = copy.deepcopy(v)
    return out


def quiz_settings(saved: Optional[dict]) -> dict:
    """The round's settings, resolved: the defaults with what was saved on top."""
    return merge(QUIZ_DEFAULTS, saved if isinstance(saved, dict) else None)


def _num(v, lo, hi, field, *, integer=False):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise _bad(field, f"{field} must be a number.")
    if integer and int(v) != v:
        raise _bad(field, f"{field} must be a whole number.")
    if not (lo <= v <= hi):
        raise _bad(field, f"{field} must be between {lo} and {hi}.")
    return int(v) if integer else round(float(v), 2)


def _one(v, allowed, field):
    if v not in allowed:
        raise _bad(field, f"{field} is one of: {', '.join(str(a) for a in allowed)}.")
    return v


def _flag(v, field):
    if not isinstance(v, bool):
        raise _bad(field, f"{field} is true or false.")
    return v


def clean_steps(steps) -> list:
    if not isinstance(steps, list) or not (1 <= len(steps) <= 5):
        raise _bad("scoring.steps", "Give one to five speed steps.")
    out, last = [], 0.0
    for i, s in enumerate(steps):
        if not isinstance(s, dict):
            raise _bad("scoring.steps", "Each step has `until` and `share`.")
        until = _num(s.get("until"), 0.01, 1.0, f"scoring.steps.{i}.until")
        share = _num(s.get("share"), 0.0, 1.0, f"scoring.steps.{i}.share")
        if until <= last:
            raise _bad("scoring.steps", "Each step must end later than the one before.")
        last = until
        out.append({"until": until, "share": share})
    if out[-1]["until"] != 1.0:
        raise _bad("scoring.steps", "The last step must run to the end of the time (until 1.0).")
    return out


def clean_teams(teams: dict) -> dict:
    mode = _one(teams.get("mode"), ("off", "pick", "auto"), "teams.mode")
    how = _one(teams.get("score"), ("total", "average"), "teams.score")
    raw = teams.get("list") or []
    if not isinstance(raw, list) or len(raw) > 8:
        raise _bad("teams.list", "Up to eight teams.", "QUIZ_TEAMS")
    out, seen = [], set()
    for i, t in enumerate(raw):
        if not isinstance(t, dict):
            raise _bad(f"teams.list.{i}", "Each team has a key, a name and a colour.", "QUIZ_TEAMS")
        key = str(t.get("key") or "").strip().lower()
        name = " ".join(str(t.get("name") or "").split())
        color = str(t.get("color") or "").strip()
        if not _TEAM_KEY.match(key) or key in seen:
            raise _bad(f"teams.list.{i}.key", "A team's key is 1 to 16 letters, digits, - or _, different for each team.", "QUIZ_TEAMS")
        if not (1 <= len(name) <= 40):
            raise _bad(f"teams.list.{i}.name", "A team's name is 1 to 40 characters.", "QUIZ_TEAMS")
        if not _HEX.match(color):
            raise _bad(f"teams.list.{i}.color", "A team's colour is #RRGGBB.", "QUIZ_TEAMS")
        seen.add(key)
        out.append({"key": key, "name": name, "color": color.upper()})
    if mode != "off" and len(out) < 2:
        raise _bad("teams.list", "Name at least two teams to play in teams.", "QUIZ_TEAMS")
    return {"mode": mode, "score": how, "list": out}


def clean_quiz_settings(resolved: dict) -> dict:
    """Check every field of a resolved settings object; the cleaned whole (422 otherwise)."""
    s = resolved.get("scoring") or {}
    f = resolved.get("flow") or {}
    p = resolved.get("players") or {}
    unknown = set(resolved) - set(QUIZ_DEFAULTS)
    if unknown:
        raise _bad(sorted(unknown)[0], f"Unknown setting: {sorted(unknown)[0]}.")
    out = {
        "scoring": {
            "points": _num(s.get("points"), 0, 10000, "scoring.points", integer=True),
            "speed": _one(s.get("speed"), ("off", "linear", "steps"), "scoring.speed"),
            "speed_share": _num(s.get("speed_share"), 0.0, 1.0, "scoring.speed_share"),
            "steps": clean_steps(s.get("steps")),
            "wrong_penalty": _one(s.get("wrong_penalty"), (0, 0.25, 0.5), "scoring.wrong_penalty"),
            "allow_negative": _flag(s.get("allow_negative"), "scoring.allow_negative"),
            "streak_from": _num(s.get("streak_from"), 0, 20, "scoring.streak_from", integer=True),
            "streak_bonus": _num(s.get("streak_bonus"), 0, 5000, "scoring.streak_bonus", integer=True),
        },
        "flow": {
            "advance": _one(f.get("advance"), ("host", "auto"), "flow.advance"),
            "auto_seconds": _num(f.get("auto_seconds"), 3, 60, "flow.auto_seconds", integer=True),
            "reveal": _one(f.get("reveal"), ("time_up", "host"), "flow.reveal"),
            "board": _one(f.get("board"), ("every", "every_n", "end"), "flow.board"),
            "board_every": _num(f.get("board_every"), 2, 20, "flow.board_every", integer=True),
        },
        "players": {
            "names": _one(p.get("names"), ("nickname", "registered"), "players.names"),
            "filter_names": _flag(p.get("filter_names"), "players.filter_names"),
            "late_join": _flag(p.get("late_join"), "players.late_join"),
            "one_per_ticket": _flag(p.get("one_per_ticket"), "players.one_per_ticket"),
            "anonymous_screen": _flag(p.get("anonymous_screen"), "players.anonymous_screen"),
        },
        "teams": clean_teams(resolved.get("teams") or {}),
        "ties": _one(resolved.get("ties"), ("join", "time"), "ties"),
        "overall": _flag(resolved.get("overall"), "overall"),
    }
    if out["scoring"]["streak_from"] == 1:
        raise _bad("scoring.streak_from", "A streak starts at two right answers in a row (or 0 for none).")
    return out


def saved_change(saved: Optional[dict], change: Optional[dict]) -> Optional[dict]:
    """What to store after a PATCH: the saved settings with `change` merged in,
    checked whole. None when nothing was ever set and nothing is now."""
    if not change and not saved:
        return None
    merged = merge(saved or {}, change or {})
    clean_quiz_settings(merge(QUIZ_DEFAULTS, merged))
    return merged


def scores_can_fall(settings: dict) -> bool:
    """Whether a player's score can go down in this round (negative marking):
    the board's sorted set in Redis holds only scores that never fall."""
    return float(settings["scoring"].get("wrong_penalty") or 0) > 0


# ─── A question ───────────────────────────────────────────────────────────

TRUE_FALSE_OPTIONS = [{"key": "a", "label": "True"}, {"key": "b", "label": "False"}]
SECONDS_MAX = 300


def _q_bad(field: str, message: str) -> CustomHTTPException:
    return CustomHTTPException(422, message=message, error_code="QUESTION_INVALID", errors={"field": field})


def _options(raw, field: str = "options", lo: int = 2, hi: int = 6) -> list:
    opts = []
    for o in raw or []:
        key = (o.get("key") if isinstance(o, dict) else getattr(o, "key", None)) or ""
        label = (o.get("label") if isinstance(o, dict) else getattr(o, "label", None)) or ""
        opts.append({"key": str(key).strip(), "label": " ".join(str(label).split())[:120]})
    keys = [o["key"] for o in opts]
    if not (lo <= len(opts) <= hi) or len(set(keys)) != len(keys) or any(not k or len(k) > 2 for k in keys) or any(not o["label"] for o in opts):
        raise _q_bad(field, f"Give the question {lo} to {hi} answers, each with text and a different key.")
    return opts


def clean_question(data: dict) -> dict:
    """A question's stored form from the console's whole question (kind,
    prompt, options, the right answer and the overrides): (prompt, options,
    correct_key, seconds, kind, settings) as a dict. 422 QUESTION_INVALID
    (or today's messages for a single-answer question) when it does not hold."""
    kind = data.get("kind") or scoring.SINGLE
    if kind not in scoring.KINDS:
        raise _q_bad("kind", f"kind is one of: {', '.join(scoring.KINDS)}.")
    prompt = " ".join(str(data.get("prompt") or "").split())
    if not (3 <= len(prompt) <= 300):
        raise _q_bad("prompt", "The question is 3 to 300 characters.")
    seconds = data.get("seconds") if data.get("seconds") is not None else 20
    if isinstance(seconds, bool) or not isinstance(seconds, int) or not (5 <= seconds <= SECONDS_MAX):
        raise _q_bad("seconds", f"Give the question 5 to {SECONDS_MAX} seconds.")
    st: dict = {}
    points = data.get("points")
    if points is not None:
        if isinstance(points, bool) or not isinstance(points, int) or not (0 <= points <= 10000):
            raise _q_bad("points", "A question's points are 0 to 10000.")
        st["points"] = points
    if data.get("double"):
        st["double"] = True
    correct_key = ""
    if kind == scoring.SINGLE:
        options = _options(data.get("options"))
        correct_key = str(data.get("correct_key") or "")
        if correct_key not in [o["key"] for o in options]:
            raise CustomHTTPException(422, message="Mark one of the answers as the right one.", error_code="QUESTION_INVALID", errors={"field": "correct_key"})
    elif kind == scoring.TRUE_FALSE:
        options = _options(data.get("options") or TRUE_FALSE_OPTIONS, lo=2, hi=2)
        correct_key = str(data.get("correct_key") or "")
        if correct_key not in [o["key"] for o in options]:
            raise _q_bad("correct_key", "Say whether it is true or false.")
    elif kind == scoring.MULTI:
        options = _options(data.get("options"))
        keys = [o["key"] for o in options]
        correct = [str(k) for k in (data.get("correct_keys") or [])]
        if not correct or len(set(correct)) != len(correct) or any(k not in keys for k in correct):
            raise _q_bad("correct_keys", "Mark one or more of the answers as right.")
        st["correct_keys"] = [k for k in keys if k in correct]
        st["multi_rule"] = _one_q(data.get("multi_rule") or "all", ("all", "proportional", "subtract"), "multi_rule")
        correct_key = st["correct_keys"][0]
    elif kind == scoring.TYPE:
        if data.get("options"):
            raise _q_bad("options", "A typed answer has no options; list the accepted answers instead.")
        options = []
        accepted = []
        for a in data.get("accepted") or []:
            v = " ".join(str(a or "").split())
            if not (1 <= len(v) <= 120):
                raise _q_bad("accepted", "Each accepted answer is 1 to 120 characters.")
            if v not in accepted:
                accepted.append(v)
        if not (1 <= len(accepted) <= 10):
            raise _q_bad("accepted", "List one to ten accepted answers.")
        st["accepted"] = accepted
        st["match"] = _one_q(data.get("match") or "exact", ("exact", "close"), "match")
    elif kind == scoring.ORDER:
        options = _options(data.get("options"))
        st["order_rule"] = _one_q(data.get("order_rule") or "all", ("all", "partial"), "order_rule")
    else:  # poll
        options = _options(data.get("options"))
    return {
        "prompt": prompt,
        "options": options,
        "correct_key": correct_key,
        "seconds": seconds,
        "kind": None if kind == scoring.SINGLE else kind,
        "settings": st or None,
    }


def _one_q(v, allowed, field):
    if v not in allowed:
        raise _q_bad(field, f"{field} is one of: {', '.join(allowed)}.")
    return v


def question_fields(row) -> dict:
    """What a stored question is, as one dict (a row or the room's dict)."""
    get = row.get if isinstance(row, dict) else (lambda k: getattr(row, k, None))
    st = get("settings") or {}
    kind = get("kind") or scoring.SINGLE
    return {
        "kind": kind,
        "correct_key": get("correct_key") if kind in (scoring.SINGLE, scoring.TRUE_FALSE) else None,
        "correct_keys": list(st.get("correct_keys") or []) if kind == scoring.MULTI else None,
        "accepted": list(st.get("accepted") or []) if kind == scoring.TYPE else None,
        "match": (st.get("match") or "exact") if kind == scoring.TYPE else None,
        "multi_rule": (st.get("multi_rule") or "all") if kind == scoring.MULTI else None,
        "order_rule": (st.get("order_rule") or "all") if kind == scoring.ORDER else None,
        "points": st.get("points"),
        "double": bool(st.get("double")),
    }


def check_response(question: dict, body: dict) -> dict:
    """The answer's payload for this question's kind, checked: exactly the
    fields its kind takes (CONTRACT 5.3). 422 ANSWER_INVALID otherwise."""
    kind = question.get("kind") or scoring.SINGLE
    keys = [o["key"] for o in question.get("options") or []]

    def bad(message: str = "That is not one of the answers.") -> CustomHTTPException:
        return CustomHTTPException(422, message=message, error_code="ANSWER_INVALID")

    if kind in scoring.ONE_KEY:
        k = body.get("option_key")
        if not isinstance(k, str) or k not in keys:
            raise bad()
        return {"option_key": k}
    if kind == scoring.MULTI:
        ks = body.get("option_keys")
        if not isinstance(ks, list) or not ks or len(set(ks)) != len(ks) or any(k not in keys for k in ks):
            raise bad("Pick one or more of the answers.")
        return {"option_keys": [k for k in keys if k in ks]}
    if kind == scoring.TYPE:
        t = body.get("text")
        t = " ".join(t.split()) if isinstance(t, str) else ""
        if not (1 <= len(t) <= 120):
            raise bad("Type an answer (up to 120 characters).")
        return {"text": t}
    if kind == scoring.ORDER:
        order = body.get("order")
        if not isinstance(order, list) or sorted(order) != sorted(keys) or len(set(order)) != len(order):
            raise bad("Put every answer in order, each once.")
        return {"order": list(order)}
    raise bad()


# ─── A poll's settings ────────────────────────────────────────────────────

POLL_KINDS = ("choice", "words", "rating", "ranking", "text")


def poll_defaults(kind: str) -> dict:
    return {
        "max_choices": 1,
        "scale": 5,
        "style": "stars",
        "results": "live",
        "show": "both",
        "chart": "cloud" if kind == "words" else "bar",
        "hide_results": False,
        "anonymous": True,
        "change_vote": True,
        "seconds": None,
        "moderate": True,
        "option_images": [],
    }


def poll_settings(kind: str, saved: Optional[dict]) -> dict:
    out = {**poll_defaults(kind), **(saved if isinstance(saved, dict) else {})}
    if kind == "rating" and "style" not in (saved or {}) and int(out.get("scale") or 5) == 10:
        out["style"] = "numbers"
    return out


def _p_bad(field: str, message: str) -> CustomHTTPException:
    return CustomHTTPException(422, message=message, error_code="POLL_SETTINGS", errors={"field": field})


def clean_poll_settings(kind: str, options: list, merged: dict) -> dict:
    """A poll's settings (the defaults, the saved and the change merged), checked."""
    unknown = set(merged) - set(poll_defaults(kind))
    if unknown:
        raise _p_bad(sorted(unknown)[0], f"Unknown setting: {sorted(unknown)[0]}.")
    out = dict(merged)
    n = len(options)
    mc = out.get("max_choices")
    if isinstance(mc, bool) or not isinstance(mc, int) or mc < 1 or (kind == "choice" and mc > max(1, n)) or (kind != "choice" and mc != 1):
        raise _p_bad("max_choices", "max_choices is 1 to the number of answers (multiple choice only).")
    if out.get("scale") not in (5, 10) or (kind != "rating" and out.get("scale") != 5):
        raise _p_bad("scale", "A rating is out of 5 or out of 10.")
    for field, allowed in (
        ("style", ("stars", "numbers")),
        ("results", ("live", "closed")),
        ("show", ("both", "percent", "count")),
        ("chart", ("bar", "column", "donut", "cloud")),
    ):
        if out.get(field) not in allowed:
            raise _p_bad(field, f"{field} is one of: {', '.join(allowed)}.")
    if out["chart"] == "cloud" and kind != "words":
        raise _p_bad("chart", "Only a word cloud is drawn as a cloud.")
    for field in ("hide_results", "anonymous", "change_vote", "moderate"):
        if not isinstance(out.get(field), bool):
            raise _p_bad(field, f"{field} is true or false.")
    sec = out.get("seconds")
    if sec is not None and (isinstance(sec, bool) or not isinstance(sec, int) or not (10 <= sec <= 3600)):
        raise _p_bad("seconds", "A poll's timer is 10 seconds to an hour, or none.")
    imgs = out.get("option_images") or []
    if not isinstance(imgs, list) or len(imgs) > max(n, 0) or any(i is not None and not isinstance(i, str) for i in imgs):
        raise _p_bad("option_images", "Give one image (or none) per answer, in order.")
    out["option_images"] = list(imgs)
    return out


def poll_settings_saved(kind: str, options: list, saved: Optional[dict], change: Optional[dict]) -> Optional[dict]:
    """What to store for a poll's settings after a change (None when never set)."""
    if not change and not saved:
        return None
    merged = {**(saved or {}), **(change or {})}
    clean_poll_settings(kind, options, poll_settings(kind, merged))
    return merged


def poll_needs_update(kind: str, st: dict) -> bool:
    """Whether a client from before this release cannot answer the poll at all."""
    if kind in ("ranking", "text"):
        return True
    return kind == "rating" and int(st.get("scale") or 5) != 5


def poll_results_hidden(st: dict, status: str) -> bool:
    """Whether guests see the results now."""
    return bool(st.get("hide_results")) or (st.get("results") == "closed" and status == "live")
