"""The console's bodies as a console from before this release reads them.

The club's endpoints answer a console of this release (X-Live-Version: 2)
with every new field; one without the header gets exactly the fields it
always did, so a console tab left open across the deploy, and every test of
the old console, sees the room as before. Each function takes the v2 body
(already JSON-ready) and keeps today's fields only.
"""

from __future__ import annotations

from typing import Optional

HOST_KEYS = ("status", "join_code", "title", "players", "current_index", "total", "question", "leaderboard")
HOST_QUESTION_KEYS = ("id", "prompt", "options", "correct_key", "seconds", "sort_order", "time_left", "opened_at", "answered", "counts")
POLL_KEYS = ("id", "kind", "prompt", "options", "status", "launched_at", "closed_at", "sort_order", "results", "my_response")
POLL_RESULT_KEYS = ("responses", "choices", "words", "rating")
QUIZ_KEYS = ("id", "title", "intro", "status", "join_code", "current_index", "players", "questions")
QUESTION_KEYS = ("id", "prompt", "options", "correct_key", "seconds", "sort_order")
SCREEN_KEYS = ("event", "pinned", "board", "poll", "quiz", "update")


def _pick(d: dict, keys, extra=("notified",)) -> dict:
    return {k: d.get(k) for k in keys if k in d} | {k: d[k] for k in extra if k in d}


def host(d: Optional[dict]) -> Optional[dict]:
    if d is None:
        return None
    out = _pick(d, HOST_KEYS)
    q = out.get("question")
    if isinstance(q, dict):
        out["question"] = {k: q.get(k) for k in HOST_QUESTION_KEYS if k in q}
    out["leaderboard"] = [{k: r[k] for k in ("rank", "name", "score") if k in r} for r in out.get("leaderboard") or []]
    return out


def poll(d: Optional[dict]) -> Optional[dict]:
    if d is None:
        return None
    out = _pick(d, POLL_KEYS)
    out.setdefault("my_response", None)
    results = d.get("results") or {}
    out["results"] = {k: v for k, v in results.items() if k in POLL_RESULT_KEYS}
    return out


def question(d: dict) -> dict:
    return {k: d.get(k) for k in QUESTION_KEYS}


def quiz(d: dict) -> dict:
    out = _pick(d, QUIZ_KEYS, extra=())
    out["questions"] = [question(q) for q in d.get("questions") or []]
    return out


def screen(d: dict) -> dict:
    """Today's screen: the poll that is up and the round that runs, nothing
    a console of this release may keep on it (a closed poll, an ended round)."""
    out = _pick(d, SCREEN_KEYS, extra=())
    p, q = d.get("poll"), d.get("quiz")
    out["poll"] = poll(p) if isinstance(p, dict) and p.get("status") == "live" else None
    out["quiz"] = host(q) if isinstance(q, dict) and q.get("status") == "live" else None
    return out


def club_view(view: str, data):
    """A club stream view's body, today's fields only."""
    if view == "host":
        return host(data)
    if view == "screen":
        return screen(data)
    if view == "polls":
        return [poll(p) for p in data]
    return data
