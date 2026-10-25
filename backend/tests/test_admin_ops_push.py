"""The push tools the portal now drives (app/api/notifications/router.py):
a push to every device is the owner's alone; the health reads are any admin's."""
from sqlalchemy import select

from app.api.audit.models import AdminAuditLogs
from app.api.users.models import UserDeviceTokens
from tests.admin_ops_support import role_headers

API = "/api/v1"


async def test_only_an_owner_pushes_to_every_device_and_it_is_audited(client, session, make_admin, make_user, mint_token, monkeypatch):
    from app.core.notifications import fanout

    queued = []

    async def enqueue_broadcast(session_, ref, title, body, image_url):
        queued.append((title, body, image_url))

    monkeypatch.setattr(fanout, "enqueue_broadcast", enqueue_broadcast)
    person = await make_user(email="device-owner@example.com")
    session.add(UserDeviceTokens(user_id=person.id, fcm_token="tok-" + "x" * 40, platform="android"))
    await session.commit()
    h = await role_headers(make_admin, mint_token)
    body = {"title": "Fest season", "body": "See what's on this week"}
    for role in ("viewer", "support"):
        assert (await client.post(f"{API}/notifications/test/send-all", json=body, headers=h[role])).status_code == 403
    r = await client.post(f"{API}/notifications/test/send-all", json=body, headers=h["owner"])
    assert r.status_code == 200, r.text
    assert r.json()["tokens_count"] == 1 and queued == [("Fest season", "See what's on this week", None)]
    audit = await session.scalar(select(AdminAuditLogs).where(AdminAuditLogs.action == "notifications.broadcast"))
    assert audit is not None and audit.details["devices"] == 1


async def test_the_health_reads_are_open_to_every_admin(client, make_admin, mint_token):
    h = await role_headers(make_admin, mint_token)
    for path in ("/notifications/test/tokens", "/notifications/topics/adoption", "/notifications/test/firebase-status"):
        r = await client.get(f"{API}{path}", headers=h["viewer"])
        assert r.status_code == 200, (path, r.text)
