"""A person's picture: a preset avatar they pick (POST /user/avatar/select)
or a photo they upload (PUT /user/profile/update-picture).

Everything that shows a person prefers the photo (reports.avatar, the
followers list, People Like You), so picking an avatar has to take the photo
away -- it used to stay, and the pick seemed to do nothing. Picking also
failed (400) for an account with no profile row yet, a removed avatar stayed
pickable, and an upload whose type was not an image was a 500.

Nothing reaches S3: the two columns' clients are stood in for, and a
picture's deletion is recorded rather than sent.
"""
import pytest

from app.api.users.models import UserAvatars, UserProfiles
from app.core.storage import fields
from tests.conftest import auth_header

PHOTO = UserProfiles.__table__.c.profile_pic.type
AVATAR = UserAvatars.__table__.c.image.type
HEX = "0123456789abcdef0123456789abcdef"


def _key(field, name: str) -> str:
    """A key as the column stores it: `name` under the column's folder."""
    return f"{field.base_path.rstrip('/')}/{name}"


@pytest.fixture
def bucket(monkeypatch):
    """What the photo and avatar columns would have done to S3: the keys put,
    and the originals of the pictures deleted."""
    seen = {"put": [], "deleted": []}

    def upload_fileobj(fileobj, bucket_name, key, **kwargs):
        seen["put"].append(key)

    for field in (PHOTO, AVATAR):
        monkeypatch.setattr(field.s3_client, "upload_fileobj", upload_fileobj)
        monkeypatch.setattr(field.s3_client, "delete_object", lambda **kw: {})
    monkeypatch.setattr(fields.S3Image, "delete", lambda self: seen["deleted"].append(self.variations["original"]))
    return seen


@pytest.fixture
async def person(session, make_user, mint_token, bucket):
    """Someone with a profile, a photo they uploaded, and two avatars to pick from."""
    user = await make_user(email="pic@example.com")
    photo = _key(PHOTO, f"v-{HEX}.png")
    session.add(UserProfiles(user_id=user.id, full_name="Pic Person", profile_pic=photo))
    fox = UserAvatars(name="Fox", image=_key(AVATAR, f"v-{'1' * 32}.png"))
    owl = UserAvatars(name="Owl", image=_key(AVATAR, f"v-{'2' * 32}.png"))
    session.add_all([fox, owl])
    await session.commit()
    return dict(user=user, photo=photo, fox=fox, owl=owl, h=auth_header(await mint_token(user)))


async def _call(client, session, method: str, path: str, **kwargs):
    """A request that starts with nothing loaded, as in production: the
    test's rows are read back from the database, pictures and all."""
    session.expunge_all()
    return await client.request(method, f"/api/v1/user{path}", **kwargs)


async def _pick(client, session, h, avatar_id):
    return await _call(client, session, "POST", "/avatar/select", json={"avatar_id": avatar_id}, headers=h)


async def _profile(client, session, h) -> dict:
    r = await _call(client, session, "GET", "/profile/me", headers=h)
    assert r.status_code == 200, r.text
    return r.json()["profile"]


# ─── Picking a preset ─────────────────────────────────────────────────────


async def test_picking_an_avatar_replaces_the_uploaded_photo(client, session, person, bucket):
    r = await _pick(client, session, person["h"], person["fox"].id)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == person["fox"].id and body["name"] == "Fox"
    assert set(body["image"]) >= {"thumbnail", "medium", "large", "original"}

    profile = await _profile(client, session, person["h"])
    assert profile["profile_pic"] is None
    assert profile["avatar"]["id"] == person["fox"].id
    # The photo's files go from the bucket too, once the change is saved.
    assert bucket["deleted"] == [person["photo"]]


async def test_picking_another_avatar_without_a_photo_deletes_nothing(client, session, person, bucket):
    await _pick(client, session, person["h"], person["fox"].id)
    r = await _pick(client, session, person["h"], person["owl"].id)
    assert r.status_code == 200, r.text
    assert r.json()["id"] == person["owl"].id
    assert bucket["deleted"] == [person["photo"]]  # once, by the first pick
    assert (await _profile(client, session, person["h"]))["avatar"]["name"] == "Owl"


async def test_clearing_the_avatar_keeps_the_photo(client, session, person, bucket):
    r = await _pick(client, session, person["h"], None)
    assert r.status_code == 200, r.text
    assert r.json() is None
    profile = await _profile(client, session, person["h"])
    assert profile["avatar"] is None and profile["profile_pic"] is not None
    assert bucket["deleted"] == []


async def test_an_account_without_a_profile_can_pick_one(client, session, make_user, mint_token, person):
    newcomer = await make_user(email="fresh@example.com")
    await session.commit()
    h = auth_header(await mint_token(newcomer))
    r = await _pick(client, session, h, person["owl"].id)
    assert r.status_code == 200, r.text
    assert r.json()["id"] == person["owl"].id
    profile = await _profile(client, session, h)
    assert profile["avatar"]["id"] == person["owl"].id and profile["full_name"] == "Test User"


async def test_a_removed_avatar_is_not_offered_and_cannot_be_picked(client, session, person, bucket):
    person["owl"].soft_delete()
    await session.commit()

    r = await _call(client, session, "GET", "/avatar/list")
    assert r.status_code == 200, r.text
    assert [a["name"] for a in r.json()] == ["Fox"]

    r = await _pick(client, session, person["h"], person["owl"].id)
    assert r.status_code == 404, r.text
    assert r.json()["error_code"] == "AVATAR_NOT_FOUND"
    # Nothing changed: the photo stays, and no avatar was set.
    profile = await _profile(client, session, person["h"])
    assert profile["avatar"] is None and profile["profile_pic"] is not None
    assert bucket["deleted"] == []


async def test_an_avatar_that_never_existed_is_a_404(client, session, person):
    r = await _pick(client, session, person["h"], 999_999)
    assert r.status_code == 404, r.text


async def test_the_picker_keeps_its_order_when_an_avatar_is_renamed(client, session, person):
    # An UPDATE writes the row anew at the end of the table, and without an
    # ORDER BY the list followed the table: a renamed avatar jumped to the end.
    person["fox"].name = "Red Fox"
    await session.commit()
    r = await _call(client, session, "GET", "/avatar/list")
    assert r.status_code == 200, r.text
    assert [a["name"] for a in r.json()] == ["Red Fox", "Owl"]
