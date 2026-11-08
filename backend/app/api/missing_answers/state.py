"""Where each person stands on each of the event's CURRENT questions
(feature-register/missing-answers-contract.md §0).

- hidden: a condition (show_if) makes it not apply to them;
- answered: answered, and the answer is valid for the question as it is now;
- invalid: answered, but no longer valid (a choice no longer among the
  options, a value of the wrong type);
- blank: it applies to them and they have no answer.

`missing` is a required question (required when shown) that is blank or
invalid: what the counts, filters and badges use. `askable` is blank or
invalid, required or not: what an ask may ask for.

Visibility is questions.visibility, the one algorithm every repo runs; an
answer's validity is the registration form's own check (answers._problem).
Nothing here reads the database or changes an answer.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Iterable, Optional

from app.api.events import questions as qmod
from app.api.events.registration import answers as answers_mod

HIDDEN, ANSWERED, INVALID, BLANK = "hidden", "answered", "invalid", "blank"
STATES = (HIDDEN, ANSWERED, INVALID, BLANK)
#: Kept beside the answers by the form itself, never a question: not stray.
NOT_STRAY = frozenset({"organization"})


def blank(value) -> bool:
    return answers_mod._blank(value)


class Form:
    """An event's current questions, as stored, read once for many people."""

    def __init__(self, questions):
        self.questions = [q for q in (questions or []) if isinstance(q, dict) and isinstance(q.get("key"), str)]
        self.keys = [q["key"] for q in self.questions]
        self.by_key = {q["key"]: q for q in self.questions}
        self._fields = {
            q["key"]: SimpleNamespace(field_type=qmod.kind_of(q), options=q.get("options")) for q in self.questions
        }

    def __contains__(self, key) -> bool:
        return key in self.by_key

    def label(self, key: str) -> str:
        q = self.by_key.get(key) or {}
        label = q.get("label")
        return label.strip() if isinstance(label, str) and label.strip() else key

    def required(self, key: str) -> bool:
        return bool((self.by_key.get(key) or {}).get("required", True))

    def in_order(self, keys: Iterable[str]) -> list[str]:
        """These keys, the form's own first in its order, each once."""
        wanted = set(keys)
        return [k for k in self.keys if k in wanted]

    def problem(self, key: str, value) -> Optional[str]:
        """What is wrong with a non-blank answer to this question, or None."""
        field = self._fields.get(key)
        return None if field is None else answers_mod._problem(field, value)

    def states(self, answers: Optional[dict]) -> dict[str, str]:
        answers = answers if isinstance(answers, dict) else {}
        shown = qmod.visibility(self.questions, answers)
        out: dict[str, str] = {}
        for key in self.keys:
            if not shown.get(key, True):
                out[key] = HIDDEN
                continue
            value = answers.get(key)
            if blank(value):
                out[key] = BLANK
            elif self.problem(key, value):
                out[key] = INVALID
            else:
                out[key] = ANSWERED
        return out

    def missing(self, answers: Optional[dict]) -> list[str]:
        """The required questions they have not validly answered, in form order."""
        return [k for k, s in self.states(answers).items() if s in (BLANK, INVALID) and self.required(k)]

    def askable(self, answers: Optional[dict], keys: Optional[Iterable[str]] = None) -> list[str]:
        """Of `keys` (every question when None), those blank or invalid for them, in form order."""
        states = self.states(answers)
        wanted = None if keys is None else set(keys)
        return [k for k, s in states.items() if s in (BLANK, INVALID) and (wanted is None or k in wanted)]

    def stray(self, answers: Optional[dict]) -> list[str]:
        """Keys they have an answer under that the form no longer asks."""
        answers = answers if isinstance(answers, dict) else {}
        return [k for k, v in answers.items() if k not in self.by_key and k not in NOT_STRAY and not blank(v)]

    def current(self, answers: Optional[dict]) -> dict:
        """Their answers to the form's own questions, nothing else."""
        answers = answers if isinstance(answers, dict) else {}
        return {k: answers[k] for k in self.keys if k in answers and not blank(answers[k])}

    def descendants(self, keys: Iterable[str]) -> list[str]:
        """The questions a condition chain ties to any of `keys` (their
        children, grandchildren...), in form order: what an answer to them
        can reveal."""
        found = set(keys)
        out: list[str] = []
        for q in self.questions:
            cond = qmod.condition_of(q)
            if cond is not None and cond["key"] in found and q["key"] not in found:
                found.add(q["key"])
                out.append(q["key"])
        return out

    def missing_brief(self, answers: Optional[dict]) -> list[dict]:
        """`missing` as the door shows it: [{"key", "label"}]."""
        return [{"key": k, "label": self.label(k)} for k in self.missing(answers)]
