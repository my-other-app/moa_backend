"""Shared set-up for the team desk's tests (tests/test_support_*.py,
tests/test_leads.py): the teams and their people, and every way out --
mail, pushes, the bucket -- kept in memory. Nothing reaches AWS or FCM."""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
import pytest_asyncio

from app.api.support import mail, reference, store, teams
from app.api.users.models import AdminRoles
from tests.conftest import auth_header

API = "/api/v1"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PDF = b"%PDF-1.4\n%fake\n"


@pytest.fixture(autouse=True)
def _reference_fresh():
    """Each test's transaction is rolled back, the reference rows with it."""
    reference.reset_for_tests()
    yield
    reference.reset_for_tests()


@pytest.fixture
def outbox(monkeypatch):
    """Every mail the desk sends, as it was handed to the provider."""
    sent: list[dict] = []
    fail: dict = {}

    def deliver(to, subject, text_body, html_body, *, key, headers=None, files=None, reply_to=None, from_name=mail.DESK_NAME,
                sender=None):
        if fail.get("error") is not None:
            raise fail["error"]
        sent.append({"to": to, "subject": subject, "text": text_body, "html": html_body, "key": key, "headers": dict(headers or {}),
                     "files": list(files or []), "from_name": from_name})
        return {"MessageId": f"0109019a-{len(sent):04d}-desk", "Provider": "ses"}

    monkeypatch.setattr(mail, "_deliver", deliver)
    box = SimpleNamespace(sent=sent, fail=fail)
    return box


@pytest.fixture
def pushes(monkeypatch):
    told: list[dict] = []

    async def send(session, user_id, title, body, data=None):
        told.append({"user_id": user_id, "title": title, "body": body, "data": data})
        return True

    from app.core.notifications import service as push

    monkeypatch.setattr(push, "send_notification_to_user", send)
    return told


@pytest.fixture
def desk_files(monkeypatch):
    kept: dict[str, bytes] = {}
    monkeypatch.setattr(store, "put", lambda key, data, ctype: kept.__setitem__(key, data))
    monkeypatch.setattr(store, "get", lambda key: kept.get(key))

    def delete(keys):
        keys = list(keys)
        for k in keys:
            kept.pop(k, None)
        return len(keys)

    monkeypatch.setattr(store, "delete", delete)
    return kept


@pytest_asyncio.fixture
async def desk(session, make_admin, make_user, mint_token, outbox, pushes, desk_files):
    """An owner; a support member and a second one (both viewer-role admins:
    the team alone gives them the desk); a sales member and a second; an
    admin in no team; an app user."""
    async def admin(name, role):
        a = await make_admin(email=f"{name}-{uuid.uuid4().hex[:6]}@example.com", role=role)
        a.full_name = name.replace("_", " ").title()
        return a

    owner = await admin("olivia_owner", AdminRoles.owner)
    sup = await admin("sam_support", AdminRoles.viewer)
    sup2 = await admin("sara_support", AdminRoles.viewer)
    sales = await admin("sid_sales", AdminRoles.viewer)
    sales2 = await admin("sana_sales", AdminRoles.viewer)
    outsider = await admin("victor_viewer", AdminRoles.support)
    person = await make_user(email=f"asha-{uuid.uuid4().hex[:6]}@example.com")
    person.full_name = "Asha Nair"
    await session.flush()
    await teams.set_members(session, "support", [sup.id, sup2.id])
    await teams.set_members(session, "sales", [sales.id, sales2.id])
    await session.commit()
    h = {}
    for key, user in (("owner", owner), ("sup", sup), ("sup2", sup2), ("sales", sales), ("sales2", sales2), ("outsider", outsider),
                      ("person", person)):
        h[key] = auth_header(await mint_token(user))
    return SimpleNamespace(owner=owner, sup=sup, sup2=sup2, sales=sales, sales2=sales2, outsider=outsider, person=person, h=h,
                           outbox=outbox, pushes=pushes, files=desk_files)


async def ask(client, *, headers=None, topic="tickets", subject="My ticket didn't arrive", message="I paid but got no ticket.",
              email="guest@example.org", name="Gita Guest", net=None, **extra):
    """A help request: signed in with `headers`, else as a guest; `net` is
    the network address it comes from (believed from our own proxy's header)."""
    body = {"topic": topic, "subject": subject, "message": message, **extra}
    if headers is None:
        body.update(email=email, name=name)
    sent = dict(headers or {})
    if net:
        sent["X-Forwarded-For"] = net
    return await client.post(f"{API}/support/requests", json=body, headers=sent)


async def reply(client, case_id, headers, body="Here is your ticket again.", kind="reply_out", **extra):
    return await client.post(f"{API}/admin/support/cases/{case_id}/messages", json={"kind": kind, "body": body, **extra}, headers=headers)
