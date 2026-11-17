"""Every CSV export's writer: text that starts like a formula stays text.

A spreadsheet opening a CSV runs a cell that starts with = + - @ (or a tab
or a carriage return) as a formula. Whatever a person typed into a form or
a profile -- a name, a phone, an answer, a note -- is written with an
apostrophe in front when it starts like that, so the sheet shows it as
typed and never runs it. Numbers, dates and every other value are written
as they are; an amount already formatted as text ("-12.50") is passed as
`Number`, so it stays a number.

Each export still turns its own values into cells (yes/no, its date format)
before the row reaches the writer; the writer only adds the apostrophe, so
a cell that already has one is left alone and the columns and formats of
every file stay as they were. The workbooks (app/core/utils/spreadsheet.py)
need none of this: a cell's type is stored with it.
"""

from __future__ import annotations

import csv
from typing import Any, Iterable

#: What a spreadsheet reads as the start of a formula.
FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")


class Number(str):
    """Text that is a number (an amount formatted to two places): written as it is."""

    __slots__ = ()


def cell(value: Any) -> Any:
    """`value` as a CSV cell: text starting like a formula gets an apostrophe."""
    if (
        isinstance(value, str)
        and not isinstance(value, Number)
        and value.startswith(FORMULA_STARTS)
    ):
        return "'" + value
    return value


class SafeWriter:
    """csv.writer's writerow and writerows, every cell through `cell`."""

    __slots__ = ("_out",)

    def __init__(self, out):
        self._out = out

    def writerow(self, row: Iterable[Any]):
        return self._out.writerow([cell(v) for v in row])

    def writerows(self, rows: Iterable[Iterable[Any]]) -> None:
        for row in rows:
            self.writerow(row)


def writer(f, **kwargs) -> SafeWriter:
    """`csv.writer(f, **kwargs)`, with every text cell made safe."""
    return SafeWriter(csv.writer(f, **kwargs))
