"""The organiser's own changes to the guest list.

Ported from Muster's attendees module: filters a desk actually uses, a
guest in full, corrections, removal with the slug typed, adding by hand
without an email going out, and an import whose history stays on record.
"""
import io
from datetime import datetime, timezone

import pytest

from app.core.email import email as email_mod
from tests.conftest import auth_header


@pytest.fixture
def no_email(monkeypatch):
    sent = []
    monkeypatch.setattr(email_mod, "send_email_with_attachment", lambda **kw: sent.append(kw))
    return sent


@pytest.fixture
async def world(session, make_club, make_event, make_user, make_registration, mint_token):
    owner, club = await make_club(name="Yukthi", email="attendees@example.com")
    event = await make_event(
        club=club, created_by=owner, name="Build Night",
        event_datetime=datetime(2026, 11, 20, 18, 0, tzinfo=timezone.utc), duration=3.0,
    )
    a = await make_user(email="ananya-a@example.com")
    s = await make_user(email="sita-a@example.com")
    r_a = await make_registration(event=event, user=a, full_name="Ananya Iyer", phone="9876543210", is_attended=True)
    r_s = await make_registration(event=event, user=s, full_name="Sita Raman", role="speaker")
    await session.commit()
    return {"event": event, "a": r_a, "s": r_s, "club": auth_header(await mint_token(owner))}


def _base(world):
    return f"/api/v1/events/registration/{world['event'].id}"


async def test_the_list_filters_by_role_check_in_and_delivery_and_searches_phones(client, world):
    e = _base(world)
    names = lambda r: [row["full_name"] for row in r.json()["items"]]  # noqa: E731

    assert names(await client.get(f"{e}/list", params={"role": "speaker"}, headers=world["club"])) == ["Sita Raman"]
    assert names(await client.get(f"{e}/list", params={"is_attended": "false"}, headers=world["club"])) == ["Sita Raman"]
    assert names(await client.get(f"{e}/list", params={"search": "98765"}, headers=world["club"])) == ["Ananya Iyer"]
    # Nobody has been sent anything, so both are "not delivered"; sorted by name.
    assert names(await client.get(f"{e}/list", params={"delivery": "not_delivered"}, headers=world["club"])) == ["Ananya Iyer", "Sita Raman"]


async def test_a_guest_in_full_carries_what_was_sent_to_them(client, world):
    r = await client.get(f"{_base(world)}/info/{world['a'].id}", headers=world["club"])

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["full_name"] == "Ananya Iyer" and body["phone"] == "9876543210"
    assert body["is_attended"] is True and body["attended_on"] is None
    assert body["ticket_deliveries"] == [] and body["certificate_deliveries"] == []
    assert body["ticket_revoked_at"] is None


async def test_a_correction_changes_the_registration_not_the_account(client, session, world):
    r = await client.patch(
        f"{_base(world)}/info/{world['s'].id}",
        json={"full_name": "Sita R.", "phone": "9000000000", "role": "staff", "additional_details": {"T-shirt": "M"}},
        headers=world["club"],
    )
    assert r.status_code == 200, r.text
    assert r.json()["full_name"] == "Sita R." and r.json()["role"] == "staff"
    assert r.json()["additional_details"] == {"T-shirt": "M"}
    await session.refresh(world["s"].user)
    assert world["s"].user.full_name == "Test User"

    taken = await client.patch(f"{_base(world)}/info/{world['s'].id}", json={"email": "ananya-a@example.com"}, headers=world["club"])
    assert taken.status_code == 409


async def test_removal_needs_the_slug_typed_and_then_takes_them_off_the_list(client, world):
    e = _base(world)
    wrong = await client.delete(f"{e}/info/{world['s'].id}", params={"confirm_slug": "nope"}, headers=world["club"])
    assert wrong.status_code == 409

    ok = await client.delete(f"{e}/info/{world['s'].id}", params={"confirm_slug": world["event"].slug}, headers=world["club"])
    assert ok.status_code == 200, ok.text
    names = [row["full_name"] for row in (await client.get(f"{e}/list", headers=world["club"])).json()["items"]]
    assert names == ["Ananya Iyer"]


async def test_adding_by_hand_registers_them_and_emails_nobody(client, world, no_email):
    r = await client.post(
        f"{_base(world)}/attendees",
        json={"full_name": "Bala Krishnan", "email": "bala-a@example.com", "role": "speaker"},
        headers=world["club"],
    )
    assert r.status_code == 200, r.text
    assert r.json()["full_name"] == "Bala Krishnan" and r.json()["role"] == "speaker"
    assert r.json()["ticket_id"].startswith("MOA-")
    assert no_email == []

    again = await client.post(f"{_base(world)}/attendees", json={"full_name": "Bala Krishnan", "email": "bala-a@example.com"}, headers=world["club"])
    assert again.status_code == 400


async def test_an_import_is_recorded_against_the_event_and_emails_nobody(client, world, no_email):
    e = _base(world)
    csv = "full_name,email,phone,role\nChitra Nair,chitra-a@example.com,,speaker\nBroken Row,,,\n"
    r = await client.post(f"{e}/import", files={"file": ("guests.csv", csv.encode(), "text/csv")}, headers=world["club"])
    assert r.status_code == 200, r.text

    history = (await client.get(f"{e}/imports", headers=world["club"])).json()
    assert len(history) == 1
    assert history[0]["task_name"].startswith("Import guests.csv")
    assert history[0]["imported"] == 1 and history[0]["failed"] == 1
    assert no_email == []
    names = [row["full_name"] for row in (await client.get(f"{e}/list", headers=world["club"])).json()["items"]]
    assert "Chitra Nair" in names

    bad = await client.post(f"{e}/import", files={"file": ("x.csv", b"name,mail\nA,b@c.d\n", "text/csv")}, headers=world["club"])
    assert bad.status_code == 422


def _sheet(*rows) -> bytes:
    return ("full_name,email,phone,role\n" + "".join(f"{name},{email},,\n" for name, email in rows)).encode()


async def _import(client, world, filename, sheet) -> dict:
    """The import, and its entry in the history once the job has run."""
    e = _base(world)
    r = await client.post(f"{e}/import", files={"file": (filename, sheet, "text/csv")}, headers=world["club"])
    assert r.status_code == 200, r.text
    history = (await client.get(f"{e}/imports", headers=world["club"])).json()
    return next(h for h in history if h["task_name"].startswith(f"Import {filename}"))


async def _names(client, world) -> set:
    return {row["full_name"] for row in (await client.get(f"{_base(world)}/list", headers=world["club"])).json()["items"]}


FIRST = [("Chitra Nair", "chitra-a@example.com"), ("Devi Menon", "devi-a@example.com")]
NEW = [("Esha Rao", "esha-a@example.com"), ("Farah Khan", "farah-a@example.com")]


async def test_a_sheet_imported_again_adds_the_new_rows_and_skips_who_is_on_the_list(client, world, no_email):
    first = await _import(client, world, "guests.csv", _sheet(*FIRST))
    assert first["status"] == "COMPLETED" and first["imported"] == 2

    # The updated sheet: a guest already on the list at the top, last
    # time's rows, and two new ones at the end.
    again = await _import(client, world, "guests-v2.csv", _sheet(("Ananya Iyer", "ananya-a@example.com"), *FIRST, *NEW))
    assert again["status"] == "COMPLETED"
    assert again["imported"] == 2 and again["failed"] == 3
    assert again["issues"] == [
        "Row 2: 'Ananya Iyer' <ananya-a@example.com> is already on the list",
        "Row 3: 'Chitra Nair' <chitra-a@example.com> is already on the list",
        "Row 4: 'Devi Menon' <devi-a@example.com> is already on the list",
    ]
    assert await _names(client, world) == {"Ananya Iyer", "Sita Raman", "Chitra Nair", "Devi Menon", "Esha Rao", "Farah Khan"}
    assert no_email == []


async def test_a_row_that_fails_is_reported_and_the_rows_after_it_still_land(client, world, no_email, fail_once):
    from app.api.events.registration import service as registration

    failed = fail_once(registration, "register_event", when=lambda **kw: kw["email"] == "chitra-a@example.com")
    log = await _import(client, world, "guests.csv", _sheet(*FIRST, *NEW))
    assert len(failed) == 1
    assert log["status"] == "PARTIAL"
    assert log["imported"] == 3 and log["failed"] == 1
    assert log["issues"][0].startswith("Row 2: 'Chitra Nair' <chitra-a@example.com> could not be registered")
    assert await _names(client, world) == {"Ananya Iyer", "Sita Raman", "Devi Menon", "Esha Rao", "Farah Khan"}


async def test_an_import_that_stops_says_why_and_keeps_the_rows_it_did(client, world, no_email, fail_once):
    from app.api.events.registration import service as registration

    # The import's own last write fails, after every row went in.
    fail_once(registration, "update_background_task_log", when=lambda *a, **kw: kw.get("new_status") in ("COMPLETED", "PARTIAL"))
    log = await _import(client, world, "guests.csv", _sheet(*FIRST))
    assert log["status"] == "PARTIAL"
    assert log["imported"] == 2
    assert log["issues"] == ["The import stopped before the end of the file. Upload it again to add the rest."]
    assert {"Chitra Nair", "Devi Menon"} <= await _names(client, world)


async def test_a_sheet_with_no_rows_fails_and_says_so(client, world, no_email):
    log = await _import(client, world, "empty.csv", b"full_name,email,phone,role\n")
    assert log["status"] == "FAILED"
    assert log["issues"] == ["The file has no rows to import."]


# ─── Import limits (app/core/utils/excel.py) ─────────────────────────────


def _workbook(**members) -> bytes:
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name.replace("__", "/"), data)
    return buf.getvalue()


async def test_an_import_past_the_size_row_or_unpacked_limits_is_refused(client, world, monkeypatch, no_email):
    from app.config import settings

    e = _base(world)
    monkeypatch.setattr(settings, "IMPORT_MAX_MB", 1)
    big = _sheet(*[(f"Guest {i}", f"g{i}@example.com") for i in range(40_000)])
    assert len(big) > 1024 * 1024
    r = await client.post(f"{e}/import", files={"file": ("big.csv", big, "text/csv")}, headers=world["club"])
    assert r.status_code == 413 and r.json()["error_code"] == "FILE_TOO_LARGE"

    monkeypatch.setattr(settings, "IMPORT_MAX_ROWS", 3)
    rows = _sheet(*[(f"Guest {i}", f"g{i}@example.com") for i in range(4)])
    r = await client.post(f"{e}/import", files={"file": ("rows.csv", rows, "text/csv")}, headers=world["club"])
    assert r.status_code == 413 and r.json()["error_code"] == "TOO_MANY_ROWS"

    # A small workbook that would unpack to far more than its size.
    monkeypatch.setattr(settings, "IMPORT_MAX_UNZIPPED_MB", 1)
    bomb = _workbook(xl__worksheets__sheet1_xml=b"\0" * (3 * 1024 * 1024))
    assert len(bomb) < 64 * 1024
    r = await client.post(f"{e}/import", files={"file": ("bomb.xlsx", bomb, "application/octet-stream")}, headers=world["club"])
    assert r.status_code == 413 and r.json()["error_code"] == "FILE_TOO_LARGE"
    assert (await client.get(f"{e}/imports", headers=world["club"])).json() == []

    # Within the limits, as before.
    ok = _sheet(("Within Limits", "within-a@example.com"))
    r = await client.post(f"{e}/import", files={"file": ("ok.csv", ok, "text/csv")}, headers=world["club"])
    assert r.status_code == 200, r.text


async def test_the_sheet_is_parsed_on_a_worker_thread(monkeypatch):
    import threading

    from starlette.datastructures import UploadFile

    from app.core.utils import excel

    seen = []
    parse = excel.parse
    monkeypatch.setattr(excel, "parse", lambda c, f: seen.append(threading.current_thread()) or parse(c, f))
    df = await excel.read_excel(UploadFile(io.BytesIO(b"full_name,email\nA,a@example.com\n"), filename="g.csv"))
    assert list(df.columns) == ["full_name", "email"] and len(df) == 1
    assert seen and seen[0] is not threading.main_thread()


def test_a_workbook_with_too_many_parts_is_refused():
    from app.core.utils import excel
    from app.response import CustomHTTPException

    many = _workbook(**{f"part{i}": b"x" for i in range(excel.MAX_ZIP_MEMBERS + 1)})
    with pytest.raises(CustomHTTPException) as refused:
        excel.check_workbook(many)
    assert refused.value.status_code == 413
    excel.check_workbook(b"not a zip at all")  # left to the parser
