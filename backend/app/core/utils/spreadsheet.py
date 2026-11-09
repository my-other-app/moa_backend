"""The exports' Excel option: the rows a CSV export writes, as an .xlsx.

Every route that hands out a CSV takes `format=xlsx` too (`FormatQuery`).
Each builds its rows once, typed (numbers, dates, text), and writes them
either as its CSV, as it always has, or through `write_xlsx` here: an
openpyxl write-only workbook, filled a batch at a time on a worker thread
into a temporary file that `xlsx_response` streams and then removes. The
event guest list (app/api/events/registration/export.py) is written the
same way.

What a cell holds:

- Text is always text. openpyxl makes any string that starts with "=" a
  formula (and "#N/A" and the like an error), so every string goes in as a
  cell whose type is set to string: what a guest typed into a form --
  `=HYPERLINK(...)`, `+cmd|...` -- shows as they typed it and never runs.
  No apostrophe in front, as the CSVs need: in a workbook a cell's type is
  stored with it, so nothing has to be read from the text.
- Characters XML cannot carry (control characters, lone surrogates) are
  dropped rather than refused, and text past Excel's 32,767 a cell is cut.
- Numbers stay numbers; a Decimal is money, shown to two places.
- A date is an Excel date. A datetime with a zone is shown in UTC, as the
  CSVs write it, and says so in its format; one without stays as it is.
- True and False are "yes" and "no", the CSVs' words.

The first row (the header) is bold and frozen; a `Bold` row is bold too
(a statement's headings). Columns are as wide as the header and the first
rows need, within limits. A list longer than a sheet holds carries on in
another sheet, the header repeated.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import re
import tempfile
from datetime import date, datetime, time, timezone
from decimal import Decimal
from enum import Enum
from typing import Annotated, Any, AsyncIterable, Iterable, Literal

from fastapi import Query
from fastapi.responses import FileResponse
from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter
from starlette.background import BackgroundTask

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

ExportFormat = Literal["csv", "xlsx"]
#: The exports' `format` query parameter: csv (the default, the file as it
#: always was) or xlsx. Anything else is the usual 422.
FormatQuery = Annotated[
    ExportFormat,
    Query(alias="format", description="csv (the default) or xlsx: the same rows as an Excel workbook"),
]

#: Rows handed to the writer thread at a time.
BATCH = 1000
#: Rows read before the columns' widths are set (write-only: they must be
#: set before the first row is written).
SAMPLE_ROWS = 200
MIN_WIDTH, MAX_WIDTH = 8, 60
#: Excel's limits: characters in a cell, rows in a sheet, a sheet's name.
MAX_CELL = 32_767
MAX_ROWS = 1_048_576
MAX_TITLE = 31

DATE_FORMAT = "yyyy-mm-dd"
DATETIME_FORMAT = "yyyy-mm-dd hh:mm:ss"
UTC_FORMAT = 'yyyy-mm-dd hh:mm:ss "UTC"'
MONEY_FORMAT = "0.00"

#: What XML 1.0 cannot carry: C0 controls but tab, newline and return;
#: lone surrogates; U+FFFE and U+FFFF. openpyxl refuses the first
#: (IllegalCharacterError); the rest fail when the file is written.
_ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")
_TITLE_ILLEGAL = re.compile(r"[\\/?*\[\]:]")
_BOLD = Font(bold=True)
_TEXT = object()  # a cell's format marker: a string, typed as one


class Bold(list):
    """A row the workbook sets in bold (a heading). A CSV writer writes it
    as any other list."""


def clean_text(value: str) -> str:
    """Text as a cell can hold it: no characters XML cannot carry, and no
    longer than Excel reads."""
    return _ILLEGAL.sub("", value)[:MAX_CELL]


def sheet_title(title: str) -> str:
    """A sheet's name as Excel takes it: none of \\ / ? * [ ] :, at most 31
    characters, not blank."""
    title = _TITLE_ILLEGAL.sub(" ", clean_text(title)).strip().strip("'")
    return title[:MAX_TITLE] or "Sheet"


def to_cell(value: Any) -> tuple[Any, Any]:
    """A value as the workbook stores it, and its format: `_TEXT` for text
    (stored as a string, never a formula), a number format, or None."""
    if value is None or value == "":
        return None, None
    if isinstance(value, Enum):
        value = value.value
    if isinstance(value, bool):
        return ("yes" if value else "no"), _TEXT
    if isinstance(value, Decimal):
        return (float(value), MONEY_FORMAT) if value.is_finite() else (str(value), _TEXT)
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return str(value), _TEXT
        return value, None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None), UTC_FORMAT
        return value, DATETIME_FORMAT
    if isinstance(value, date):
        return value, DATE_FORMAT
    if isinstance(value, time):
        return clean_text(value.isoformat()), _TEXT
    if isinstance(value, (list, tuple)):
        value = ", ".join("" if v is None else str(v) for v in value)
    elif isinstance(value, dict):
        value = json.dumps(value, ensure_ascii=False)
    text = clean_text(value if isinstance(value, str) else str(value))
    return (text or None), (_TEXT if text else None)


def _width(value, fmt) -> int:
    if value is None:
        return 0
    if fmt is _TEXT:
        return max(len(line) for line in value.splitlines() or [""])
    if fmt == UTC_FORMAT:
        return 23
    if fmt == DATETIME_FORMAT:
        return 19
    if fmt == DATE_FORMAT:
        return 10
    if fmt == MONEY_FORMAT:
        return len(f"{value:.2f}")
    return len(str(value))


class XlsxWriter:
    """A write-only workbook of rows, the first (`headers`) bold and frozen.
    One call at a time: `write_xlsx` hands each to a worker thread in turn."""

    def __init__(self, title: str, headers: Iterable | None = None, *, freeze: int | None = None, sample: int = SAMPLE_ROWS):
        self.book = Workbook(write_only=True)
        self.title = sheet_title(title)
        self.freeze = (0 if headers is None else 1) if freeze is None else freeze
        self.sample = max(1, sample)
        self.sheet = None
        self.sheets = 0
        self._header = None if headers is None else [to_cell(v) for v in headers]
        self._widths: list[int] = []
        self._pending: list | None = []
        self._count = 0
        if self._header is not None:
            self._add(self._header, True)

    # Rows

    def append(self, row: Iterable, *, bold: bool | None = None) -> None:
        self._add([to_cell(v) for v in row], isinstance(row, Bold) if bold is None else bold)

    def extend(self, rows: Iterable[Iterable]) -> None:
        for row in rows:
            self.append(row)

    def _add(self, row: list, bold: bool) -> None:
        if self._pending is None:
            self._write(row, bold)
            return
        self._pending.append((row, bold))
        for i, (value, fmt) in enumerate(row):
            if i == len(self._widths):
                self._widths.append(0)
            self._widths[i] = max(self._widths[i], _width(value, fmt))
        if len(self._pending) >= self.sample:
            self._start()

    def _start(self) -> None:
        pending, self._pending = self._pending or [], None
        self._open_sheet()
        for row, bold in pending:
            self._write(row, bold)

    def _open_sheet(self) -> None:
        self.sheets += 1
        name = self.title if self.sheets == 1 else f"{self.title[:MAX_TITLE - 5]} ({self.sheets})"
        self.sheet = self.book.create_sheet(name)
        for i, width in enumerate(self._widths, 1):
            self.sheet.column_dimensions[get_column_letter(i)].width = min(max(width + 2, MIN_WIDTH), MAX_WIDTH)
        if self.freeze:
            self.sheet.freeze_panes = f"A{self.freeze + 1}"
        self._count = 0

    def _write(self, row: list, bold: bool) -> None:
        if self._count >= MAX_ROWS:
            # Past what a sheet holds: the rest in the next, under the header again.
            self._open_sheet()
            if self._header is not None:
                self._write(self._header, True)
        self.sheet.append([self._cell(value, fmt, bold) for value, fmt in row])
        self._count += 1

    def _cell(self, value, fmt, bold: bool):
        if value is None:
            return None
        if fmt is None and not bold:
            return value  # a plain number
        cell = WriteOnlyCell(self.sheet, value=value)
        if fmt is _TEXT:
            # Whatever the text starts with ("=", "+", "@", "#N/A"), it is a
            # string: never a formula, never an error code.
            cell.data_type = "s"
        elif fmt is not None:
            cell.number_format = fmt
        if bold:
            cell.font = _BOLD
        return cell

    # The file

    def save(self) -> str:
        """The workbook in a temporary file; the caller removes it."""
        if self._pending is not None:
            self._start()
        fd, path = tempfile.mkstemp(prefix="export-", suffix=".xlsx")
        os.close(fd)
        try:
            self.book.save(path)
        except BaseException:
            remove(path)
            self.discard()
            raise
        return path

    def discard(self) -> None:
        """What a workbook given up on left on disk: openpyxl's own
        temporary file a sheet, which only its save would remove."""
        for ws in self.book.worksheets:
            writer = getattr(ws, "_writer", None)
            if writer is None:
                continue
            with contextlib.suppress(Exception):
                ws.close()  # ends its XML, so nothing writes to it later
            with contextlib.suppress(Exception):
                writer.cleanup()


async def _rows(rows) -> AsyncIterable:
    if hasattr(rows, "__aiter__"):
        async for row in rows:
            yield row
    else:
        for row in rows:
            yield row


async def write_xlsx(
    rows: Iterable | AsyncIterable, *, title: str, headers: Iterable | None = None,
    freeze: int | None = None, batch: int = BATCH,
) -> str:
    """The rows (a list, a generator, or an async one off a cursor) as an
    .xlsx file on disk, written a batch at a time on a worker thread so the
    event loop is never held. The caller sends it and removes it
    (`xlsx_response`)."""
    writer = XlsxWriter(title, headers, freeze=freeze)
    chunk: list = []
    try:
        async for row in _rows(rows):
            chunk.append(row)
            if len(chunk) >= batch:
                await asyncio.to_thread(writer.extend, chunk)
                chunk = []
        if chunk:
            await asyncio.to_thread(writer.extend, chunk)
    except BaseException:
        writer.discard()
        raise
    return await asyncio.to_thread(writer.save)


def remove(path: str) -> None:
    with contextlib.suppress(FileNotFoundError):
        os.remove(path)


def xlsx_response(path: str, filename: str, *, cache_control: str | None = None) -> FileResponse:
    """The file as a download, streamed from disk and removed once sent."""
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    if cache_control:
        headers["Cache-Control"] = cache_control
    return FileResponse(path, media_type=XLSX_MEDIA_TYPE, headers=headers, background=BackgroundTask(remove, path))


__all__ = [
    "BATCH", "Bold", "ExportFormat", "FormatQuery", "XLSX_MEDIA_TYPE", "XlsxWriter",
    "clean_text", "sheet_title", "to_cell", "write_xlsx", "xlsx_response",
]
