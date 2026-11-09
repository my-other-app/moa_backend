"""Reading an export's Excel workbook back, for the exports' tests
(app/core/utils/spreadsheet.py)."""
import csv
import io
from datetime import datetime, timedelta, timezone

from openpyxl import load_workbook

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def workbook_of(response, filename: str | None = None):
    """The response's workbook, after checking it was sent as one."""
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == XLSX
    disposition = response.headers["content-disposition"]
    assert disposition.startswith('attachment; filename="') and disposition.endswith('.xlsx"'), disposition
    if filename is not None:
        assert disposition == f'attachment; filename="{filename}"'
    return load_workbook(io.BytesIO(response.content))


def rows_of(sheet) -> list[list]:
    """Every row's values, without the blanks openpyxl pads a short row with."""
    out = []
    for row in sheet.iter_rows():
        values = [c.value for c in row]
        while values and values[-1] is None:
            values.pop()
        out.append(values)
    return out


def records(sheet) -> list[dict]:
    """The rows under the header, each by the header's names."""
    head, *rows = [[c.value for c in row] for row in sheet.iter_rows()]
    return [dict(zip(head, row)) for row in rows]


def cell_by(sheet, header: str, row_index: int):
    """One cell (not its value): `row_index` 1 is the first row under the header."""
    head = [c.value for c in sheet[1]]
    return sheet.cell(row=row_index + 1, column=head.index(header) + 1)


def matches_csv(value, text: str) -> bool:
    """Whether a workbook's cell holds what the CSV wrote as `text`: the
    same number, the same instant (to the second), the same day, the same
    words -- less the apostrophe the CSV puts before text that looks like a
    formula, which a workbook does not need."""
    if value is None:
        return text == ""
    if isinstance(value, datetime):
        if len(text) == 10:
            return value == datetime.fromisoformat(text)
        when = datetime.fromisoformat(text.replace(" ", "T"))
        if when.tzinfo is not None:
            when = when.astimezone(timezone.utc).replace(tzinfo=None)
        return abs(value - when) < timedelta(seconds=1)
    if isinstance(value, (int, float)):
        return text != "" and float(text) == value
    return text == value or (text == "'" + value and value[:1] in ("=", "+", "-", "@", "\t", "\r"))


def assert_same_as_csv(sheet, csv_text: str) -> None:
    """The workbook carries the CSV's rows, cell for cell, in its order."""
    expected = list(csv.reader(io.StringIO(csv_text.lstrip("\ufeff"))))
    got = rows_of(sheet)
    assert len(got) == len(expected), (len(got), len(expected))
    for n, (row, line) in enumerate(zip(got, expected), 1):
        assert len(row) <= len(line), (n, row, line)
        row = row + [None] * (len(line) - len(row))
        bad = [(i, v, t) for i, (v, t) in enumerate(zip(row, line)) if not matches_csv(v, t)]
        assert not bad, (n, bad)
