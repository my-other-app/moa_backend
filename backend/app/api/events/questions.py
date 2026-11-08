"""A registration form's questions: which ones a person is shown, the rules
a form keeps, and how a question is stored and how it goes out.

The contract every client builds to is
feature-register/conditional-questions-contract.md.

A question may be shown only when an earlier choice question was answered a
certain way: `show_if: {"key": <the earlier question's key>, "any_of":
[<its answers that show it>]}`. Without `show_if` a question is always
shown. A shown question that is `required` must be answered; a hidden one is
never required, and its answer is never kept.

Shown, in list order (a question's parent always comes before it):

    visible(q) = no show_if
                 or (visible(parent) and the parent's answer matches any_of)

where an answer matches when it, or any box ticked (a list, or one option
as a string), is one of `any_of`, compared exactly as text; a blank answer
never does. The same algorithm as the dashboards', the website's and the
app's (moa_web packages/questions/src/visibility.ts).

On the wire (`wire`), a question with `show_if` goes out as `required:
false` with the club's setting in `required_when_shown`: the apps already in
people's hands show every question and enforce `required` themselves, so a
student-only question sent as required would stop a working person
registering. On a write, `required_when_shown` is the club's setting when it
is sent. Stored (`stored`), the setting is in `required`, as it always was.

Everything here takes a question as a dict (the stored form) or as an
`EventAdditionalDetail`; nothing here imports the schemas.
"""
from __future__ import annotations

from typing import Iterable, Optional

from app.response import CustomHTTPException

#: The question types another question can depend on (rule R2).
CHOICE_TYPES = ("select", "radio", "checkbox")
#: The most questions a form has (R4), the same as the most answers kept.
MAX_QUESTIONS = 50
#: The most conditions on a question's chain of parents (R4): an always-shown
#: question is 0, its child 1, its grandchild 2.
MAX_DEPTH = 5


def _get(q, name: str, default=None):
    if isinstance(q, dict):
        return q.get(name, default)
    return getattr(q, name, default)


def kind_of(q) -> str:
    """The question's type as a string, from the enum or the stored text."""
    value = _get(q, "field_type")
    return getattr(value, "value", value)


def condition_of(q) -> Optional[dict]:
    """The question's `show_if` as {"key", "any_of"}, or None when it is
    always shown."""
    cond = _get(q, "show_if")
    if cond is None:
        return None
    if not isinstance(cond, dict):
        cond = {"key": getattr(cond, "key", None), "any_of": getattr(cond, "any_of", None)}
    return {"key": cond.get("key"), "any_of": list(cond.get("any_of") or [])}


def _text(value) -> str:
    """An answer as text, as the dashboards' String(value) has it."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def blank(answer) -> bool:
    """Nothing answered: None, blank text, or no box ticked."""
    if answer is None:
        return True
    if isinstance(answer, list):
        return all(blank(v) for v in answer)
    return not _text(answer).strip()


def matches(answer, any_of: Iterable) -> bool:
    """Whether a parent's answer shows the child: the answer, or any box
    ticked (a list, or the one option as a string, as the website sends
    it), is one of `any_of`. Compared exactly, as text: no trimming, case
    and all. A blank answer never matches."""
    if blank(answer):
        return False
    wanted = [str(v) for v in (any_of or [])]
    picked = answer if isinstance(answer, list) else [answer]
    return any(v is not None and _text(v) in wanted for v in picked)


def visibility(questions, answers: Optional[dict]) -> dict[str, bool]:
    """Every question's key -> whether it is shown, given these answers. One
    pass in list order: a parent comes before its children (rule R1), and
    a condition on a question that is not before it shows nothing."""
    answers = answers if isinstance(answers, dict) else {}
    shown: dict[str, bool] = {}
    for q in questions or []:
        key = _get(q, "key")
        if not isinstance(key, str):
            continue
        cond = condition_of(q)
        if cond is None:
            shown[key] = True
        else:
            shown[key] = shown.get(cond["key"]) is True and matches(answers.get(cond["key"]), cond["any_of"])
    return shown


def prune(questions, answers: Optional[dict]) -> dict:
    """The answers without those to questions hidden by them. A key the form
    does not ask (an import's column, the organisation) is kept."""
    answers = answers if isinstance(answers, dict) else {}
    shown = visibility(questions, answers)
    return {k: v for k, v in answers.items() if shown.get(k, True)}


# ---------------------------------------------------------------------------
# Storing and sending
# ---------------------------------------------------------------------------


def stored(q: dict) -> dict:
    """The question as it is kept: the club's setting in `required` (from
    `required_when_shown` when that was sent), no `required_when_shown`,
    and no `show_if` on an always-shown one, so a form without conditions is
    stored exactly as before."""
    out = dict(q)
    when_shown = out.pop("required_when_shown", None)
    if when_shown is not None:
        out["required"] = bool(when_shown)
    if out.get("show_if") is None:
        out.pop("show_if", None)
    return out


def wire(q: dict) -> dict:
    """The question as every read sends it. One with `show_if` goes out as
    `required: false` plus `required_when_shown`: the club's setting. One
    without goes out exactly as before: no `show_if`, no
    `required_when_shown`."""
    out = stored(q)
    if out.get("show_if") is not None:
        out["required_when_shown"] = bool(out.get("required", True))
        out["required"] = False
    return out


def wire_all(questions) -> list[dict]:
    """A stored form, as every read sends it. Whatever is not a question is
    left out."""
    return [wire(q) for q in (questions or []) if isinstance(q, dict)]


# ---------------------------------------------------------------------------
# The rules a form keeps (R1-R6), on create and on every update
# ---------------------------------------------------------------------------


def name_of(q, fallback: str = "a question") -> str:
    """How a message names a question: its label in single quotes, else its
    key (the dashboards' nameOf, packages/questions/src/rules.ts)."""
    if q is None:
        return fallback
    label = _get(q, "label")
    return f"'{(label.strip() if isinstance(label, str) else '') or _get(q, 'key')}'"


def _refuse(key, message: str) -> CustomHTTPException:
    return CustomHTTPException(
        400, message=message, error_code="INVALID_FORM",
        errors={str(key or "additional_details"): message},
    )


def check_form(questions, before=None) -> None:
    """A 400 naming the question and the rule when the form breaks one: the
    first problem, in the dashboards' order and words
    (packages/questions/src/rules.ts checkDefinition), so a club reads one
    sentence whichever catches it. `before`, on an update, is the form as it
    was: an option a condition used, removed or renamed, is named as that
    (R6, the dashboards' checkOptionChange)."""
    questions = list(questions or [])
    if len(questions) > MAX_QUESTIONS:
        raise _refuse(None, f"A form can ask at most {MAX_QUESTIONS} questions; this one has {len(questions)}.")
    first_at: dict[str, int] = {}
    for i, q in enumerate(questions):
        key = _get(q, "key")
        if key in first_at:
            raise _refuse(key, f"{name_of(q)} has the same key as {name_of(questions[first_at[key]])}. Every question needs its own.")
        first_at[key] = i
    depth: dict[str, int] = {}
    for i, q in enumerate(questions):
        cond = condition_of(q)
        if cond is None:
            continue
        key = _get(q, "key")
        _check_condition(q, i, cond, questions, first_at, before)
        # The conditions on its chain of parents: an always-shown question
        # is 0, its child 1 (R4). Parents come first (R1, just checked).
        depth[key] = depth.get(cond["key"], 0) + 1
        if depth[key] > MAX_DEPTH:
            raise _refuse(key, f"{name_of(q)} is nested {depth[key]} deep. Questions can be nested at most {MAX_DEPTH} deep.")


def _check_condition(q, i: int, cond: dict, questions: list, first_at: dict, before) -> None:
    key = _get(q, "key")
    parent_key = cond["key"]
    if parent_key == key:
        raise _refuse(key, f"{name_of(q)} can't depend on itself.")
    at = first_at.get(parent_key)
    if at is None:
        raise _refuse(key, f"{name_of(q)} depends on a question that isn't in this form.")
    parent = questions[at]
    if at > i:
        raise _refuse(key, f"{name_of(q)} depends on {name_of(parent)}, which must come before it.")
    options = list(_get(parent, "options") or [])
    if kind_of(parent) not in CHOICE_TYPES or not options:
        raise _refuse(
            key, f"{name_of(q)} depends on {name_of(parent)}, which isn't a choice question. Only a question with options can show or hide others.",
        )
    if not cond["any_of"]:
        raise _refuse(key, f"{name_of(q)} needs at least one answer to {name_of(parent)} that shows it.")
    stray = next((v for v in cond["any_of"] if v not in options), None)
    if stray is not None:
        if _option_removed(before, parent_key, key, stray):
            raise _refuse(
                key, f"'{stray}' can't be removed or renamed: {name_of(q)} is shown when {name_of(parent)} is '{stray}'. Change that question's condition first.",
            )
        raise _refuse(key, f"{name_of(q)} is shown when {name_of(parent)} is '{stray}', which isn't one of its options.")


def _option_removed(before, parent_key: str, child_key: str, value) -> bool:
    """Whether the option was the parent's before this save, and the child
    already depended on it: an option removed or renamed under a condition."""
    if not before:
        return False
    old = {(_get(q, "key")): q for q in before if _get(q, "key")}
    parent, child = old.get(parent_key), old.get(child_key)
    if parent is None or child is None:
        return False
    cond = condition_of(child)
    return value in (_get(parent, "options") or []) and cond is not None and cond["key"] == parent_key and value in cond["any_of"]


def check_options(questions) -> None:
    """A 400 when a choice question (select, radio, checkbox) has no
    options: on create, and on every update (it used to be create only, so
    an edit could save a choice nobody can pick)."""
    for q in questions or []:
        if kind_of(q) in CHOICE_TYPES and not [o for o in (_get(q, "options") or []) if o is not None]:
            raise CustomHTTPException(400, message="Options required for select, radio, checkbox fields")


# ---------------------------------------------------------------------------
# Keys that stay put (feature-register/missing-answers-contract.md §1.1)
# ---------------------------------------------------------------------------


def normal_label(label) -> str:
    """A label as two labels are compared: trimmed, lower case, one space
    between words."""
    return " ".join(str(label or "").split()).lower()


def keep_keys(incoming: list[dict], before) -> list[dict]:
    """The form about to be saved, with the stored keys kept where an editor
    re-derived one from a label it did not really change.

    An incoming question whose key the stored form does not have, but whose
    label is the same (normal_label) as a stored question's whose key is no
    longer in the form, is that stored question: it keeps the stored key,
    and so the answers people already gave stay its answers. A condition on
    the renamed key (a child's show_if) follows. Only a label that is one
    stored question's and one incoming question's is taken as the same
    question; anything less certain is left as sent. Stored answers are
    never rewritten here."""
    stored = [q for q in (before or []) if isinstance(q, dict) and isinstance(q.get("key"), str)]
    sent_keys = {q.get("key") for q in incoming}
    stored_keys = {q["key"] for q in stored}
    gone: dict[str, list[str]] = {}
    for q in stored:
        if q["key"] not in sent_keys:
            gone.setdefault(normal_label(q.get("label")), []).append(q["key"])
    new: dict[str, list[int]] = {}
    for i, q in enumerate(incoming):
        if q.get("key") not in stored_keys:
            new.setdefault(normal_label(q.get("label")), []).append(i)
    renames: dict[str, str] = {}
    for label, at in new.items():
        old = gone.get(label) or []
        if label and len(at) == 1 and len(old) == 1:
            renames[incoming[at[0]]["key"]] = old[0]
    if not renames:
        return incoming
    out = []
    for q in incoming:
        q = dict(q)
        if q.get("key") in renames:
            q["key"] = renames[q["key"]]
        cond = q.get("show_if")
        if isinstance(cond, dict) and cond.get("key") in renames:
            q["show_if"] = {**cond, "key": renames[cond["key"]]}
        out.append(q)
    return out


def explain_form_error(questions, before=None) -> Optional[str]:
    """The message check_form would refuse with, or None: for callers that
    say it in their own words (the MCP tools)."""
    try:
        check_form(questions, before)
    except CustomHTTPException as e:
        return e.message
    return None
