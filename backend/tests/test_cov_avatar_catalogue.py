"""The preset avatars people pick from, kept by admins: POST /user/avatar/create,
PUT /user/avatar/update/{id}, DELETE /user/avatar/delete/{id}. Adding and
changing them is managing content (owners); removing one is destroying
(owners). Nothing reaches S3: the column's client is stood in for."""
import io

import pytest
from PIL import Image
from sqlalchemy import select

from app.api.users.models import AdminRoles, UserAvatars
from app.core.storage import fields
from tests.conftest import auth_header

AVATAR = UserAvatars.__table__.c.image.type
API = "/api/v1/user/avatar"


def _png(colour=(23, 165, 143)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (64, 64), colour).save(out, "PNG")
    return out.getvalue()


@pytest.fixture
def bucket(monkeypatch):
    seen = {"put": [], "deleted": []}
    monkeypatch.setattr(AVATAR.s3_client, "upload_fileobj", lambda fileobj, bucket_name, key, **kw: seen["put"].append(key))
    monkeypatch.setattr(AVATAR.s3_client, "delete_object", lambda **kw: {})
    monkeypatch.setattr(fields.S3Image, "delete", lambda self: seen["deleted"].append(self.variations["original"]))
    return seen


@pytest.fixture
async def people(session, make_admin, make_user, mint_token, bucket):
    owner = await make_admin(email="cov-avatar-owner@example.com", role=AdminRoles.owner)
    support = await make_admin(email="cov-avatar-support@example.com", role=AdminRoles.support)
    person = await make_user(email="cov-avatar-person@example.com")
    await session.commit()
    return dict(owner=auth_header(await mint_token(owner)), support=auth_header(await mint_token(support)), person=auth_header(await mint_token(person)))


async def _create(client, h, name, png=None, filename="fox.png", content_type="image/png"):
    return await client.post(f"{API}/create", data={"name": name}, files={"avatar": (filename, png or _png(), content_type)}, headers=h)


async def test_an_owner_adds_renames_replaces_and_removes_an_avatar(client, session, people, bucket):
    h = people["owner"]
    fox = await _create(client, h, "Fox")
    assert fox.status_code == 200, fox.text
    fox = fox.json()
    assert fox["name"] == "Fox" and fox["image"]
    assert bucket["put"] and all(k.startswith(AVATAR.base_path.rstrip("/")) for k in bucket["put"])
    owl = (await _create(client, h, "Owl", _png((200, 10, 10)))).json()
    assert [a["name"] for a in (await client.get(f"{API}/list")).json()] == ["Fox", "Owl"]  # in the order added

    # A new name alone keeps the picture.
    renamed = await client.put(f"{API}/update/{fox['id']}", data={"name": "Red Fox"}, headers=h)
    assert renamed.status_code == 200 and renamed.json()["name"] == "Red Fox" and renamed.json()["image"] == fox["image"]
    assert bucket["deleted"] == []
    # A new picture replaces the old one, which is deleted.
    put_before = len(bucket["put"])
    replaced = await client.put(f"{API}/update/{fox['id']}", files={"avatar": ("fox2.png", _png((0, 0, 255)), "image/png")}, headers=h)
    assert replaced.status_code == 200 and replaced.json()["name"] == "Red Fox"
    assert len(bucket["put"]) > put_before and len(bucket["deleted"]) == 1
    assert [a["name"] for a in (await client.get(f"{API}/list")).json()] == ["Red Fox", "Owl"]  # still where it was

    # Removed: off the list, not pickable, and gone for every later change.
    assert (await client.delete(f"{API}/delete/{owl['id']}", headers=h)).json() == {"message": "Avatar deleted successfully"}
    assert [a["name"] for a in (await client.get(f"{API}/list")).json()] == ["Red Fox"]
    assert (await client.post(f"{API}/select", json={"avatar_id": owl["id"]}, headers=people["person"])).status_code in (400, 404)
    assert (await client.delete(f"{API}/delete/{owl['id']}", headers=h)).status_code == 404
    assert (await client.put(f"{API}/update/{owl['id']}", data={"name": "Back"}, headers=h)).status_code == 404
    row = await session.scalar(select(UserAvatars).where(UserAvatars.id == owl["id"]).execution_options(include_deleted=True, populate_existing=True))
    assert row is None or row.is_deleted  # soft-deleted: the row stays for the profiles that held it


async def test_only_an_owner_keeps_the_catalogue(client, people):
    fox = (await _create(client, people["owner"], "Fox")).json()
    for who in ("support", "person"):
        h = people[who]
        assert (await _create(client, h, "Nope")).status_code == 403, who
        assert (await client.put(f"{API}/update/{fox['id']}", data={"name": "Nope"}, headers=h)).status_code == 403, who
        assert (await client.delete(f"{API}/delete/{fox['id']}", headers=h)).status_code == 403, who
    assert (await _create(client, {}, "Nope")).status_code == 401
    assert [a["name"] for a in (await client.get(f"{API}/list")).json()] == ["Fox"]


async def test_an_avatar_needs_a_short_name_and_a_picture(client, session, people):
    h = people["owner"]
    assert (await client.post(f"{API}/create", data={"name": "Fox"}, headers=h)).status_code == 422  # no picture
    assert (await client.post(f"{API}/create", files={"avatar": ("fox.png", _png(), "image/png")}, headers=h)).status_code == 422  # no name
    # The column holds twenty characters: longer is the form's mistake, not a 500.
    assert (await _create(client, h, "A name far too long for it")).status_code == 422
    assert (await _create(client, h, "Text", b"not a picture", "fox.txt", "text/plain")).status_code == 422
    await session.rollback()  # a request's own session would be gone; this one is shared with the test
    fox = await _create(client, h, "Fox")
    assert fox.status_code == 200, fox.text
    fox = fox.json()
    assert (await client.put(f"{API}/update/{fox['id']}", data={"name": "A name far too long for it"}, headers=h)).status_code == 422
    assert (await client.put(f"{API}/update/999999", data={"name": "Ghost"}, headers=h)).status_code == 404
