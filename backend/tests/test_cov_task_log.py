"""GET /background-task-log/{id}: an admin reads how a background run went
(the attendee import writes one: app/api/events/registration/service.py).
"""
import uuid

from app.api import service as task_logs
from tests.conftest import auth_header

URL = "/api/v1/background-task-log"


async def test_an_admin_reads_a_runs_status_and_its_log_as_it_grows(client, session, make_admin, mint_token):
    admin = await make_admin(email="task-log-admin@example.com")
    h = auth_header(await mint_token(admin))
    row = await task_logs.create_background_task_log(
        session, task_name="Import attendees", task_type="REGISTRATION_IMPORT",
        initial_logs=[{"message": "started", "level": "INFO"}],
    )

    r = await client.get(f"{URL}/{row.id}", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == str(row.id) and body["task_name"] == "Import attendees" and body["task_type"] == "REGISTRATION_IMPORT"
    assert body["status"] == "STARTED" and [e["message"] for e in body["logs"]] == ["started"]

    # The run appends to its log and finishes; the next read shows both.
    await task_logs.update_background_task_log(session, row.id, new_logs=[{"message": "120 rows read", "level": "INFO"}], new_status="COMPLETED")
    body = (await client.get(f"{URL}/{row.id}", headers=h)).json()
    assert body["status"] == "COMPLETED" and [e["message"] for e in body["logs"]] == ["started", "120 rows read"]


async def test_an_id_with_no_run_is_a_404(client, make_admin, mint_token):
    admin = await make_admin(email="task-log-missing@example.com")
    r = await client.get(f"{URL}/{uuid.uuid4()}", headers=auth_header(await mint_token(admin)))
    assert r.status_code == 404, r.text


async def test_only_an_admin_may_read_one(client, session, make_user, make_club, mint_token):
    row = await task_logs.create_background_task_log(session, task_name="Import attendees", task_type="REGISTRATION_IMPORT")
    guest = await make_user(email="task-log-guest@example.com")
    owner, _club = await make_club(name="Log Club", email="task-log-club@example.com")

    assert (await client.get(f"{URL}/{row.id}")).status_code == 401
    for who in (guest, owner):
        r = await client.get(f"{URL}/{row.id}", headers=auth_header(await mint_token(who)))
        assert r.status_code == 403, (who.email, r.status_code, r.text)
