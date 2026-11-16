"""SQL for the CHECK constraints the models declare."""
from __future__ import annotations

from typing import Iterable


def one_of(column: str, words: Iterable[str]) -> str:
    """A CHECK that `column` holds one of `words`: `status IN ('a', 'b')`.

    The migration that adds the constraint writes the same text, so the
    database the migrations build and the one the models build compare
    equal (tests/test_schema_matches_models.py)."""
    return f"{column} IN ({', '.join(repr(str(w)) for w in words)})"
