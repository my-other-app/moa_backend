"""On-demand database snapshots.

The endpoint hands over the whole database, so the parts worth pinning are the
ones that decide who gets it and which file they get: the capability, and the
name, which reaches the filesystem.
"""

import pytest

from tests.conftest import auth_header
from app.api.backups import service


@pytest.mark.parametrize(
    "name",
    [
        "../../etc/passwd",
        "..%2f..%2fetc%2fpasswd",
        "backup-20260101-000000.dump/../../../etc/shadow",
        "/etc/passwd",
        "backup-20260101-000000.sql",      # right shape, wrong suffix
        "anything-20260101-000000.dump",   # not a name this service makes
        "backup-2026-01-01.dump",
        "",
    ],
)
def test_a_name_that_is_not_ours_is_refused(name, tmp_path, monkeypatch):
    """
    The name comes from the URL and is joined onto a directory, so this is the
    one place a traversal would land. Rejected on the pattern, before the
    filesystem is touched at all.
    """
    monkeypatch.setattr(service.settings, "BACKUP_DIR", str(tmp_path), raising=False)

    with pytest.raises(Exception) as caught:
        service.path_for_download(name)
    assert getattr(caught.value, "status_code", None) in (400, 404)


def test_a_symlink_out_of_the_directory_is_refused(tmp_path, monkeypatch):
    """
    The pattern alone cannot see this: the name is perfectly well-formed and
    the file exists. Resolving the path and checking containment is what
    catches it.
    """
    monkeypatch.setattr(service.settings, "BACKUP_DIR", str(tmp_path), raising=False)
    secret = tmp_path.parent / "secret.txt"
    secret.write_text("not yours")
    (tmp_path / "backup-20260101-000000.dump").symlink_to(secret)

    with pytest.raises(Exception) as caught:
        service.path_for_download("backup-20260101-000000.dump")
    assert getattr(caught.value, "status_code", None) == 400


def test_listing_ignores_partial_and_foreign_files(tmp_path, monkeypatch):
    """
    A dump is written under a dotted temporary name and moved into place, so a
    crashed one must not appear in the listing as though it were restorable.
    """
    monkeypatch.setattr(service.settings, "BACKUP_DIR", str(tmp_path), raising=False)
    (tmp_path / "backup-20260101-000000.dump").write_bytes(b"x" * 10)
    (tmp_path / ".backup-20260102-000000.dump.partial").write_bytes(b"half")
    (tmp_path / "notes.txt").write_text("unrelated")

    names = [b.name for b in service.list_backups()]
    assert names == ["backup-20260101-000000.dump"]


def test_listing_is_newest_first(tmp_path, monkeypatch):
    import os, time

    monkeypatch.setattr(service.settings, "BACKUP_DIR", str(tmp_path), raising=False)
    older = tmp_path / "backup-20260101-000000.dump"
    newer = tmp_path / "backup-20260102-000000.dump"
    older.write_bytes(b"a")
    newer.write_bytes(b"b")
    os.utime(older, (time.time() - 3600, time.time() - 3600))

    assert [b.name for b in service.list_backups()][0] == newer.name


def test_a_missing_directory_lists_as_empty_rather_than_failing(tmp_path, monkeypatch):
    # Before the first snapshot the mount point may exist with nothing in it,
    # or not yet exist at all. Neither is an error worth showing an admin.
    monkeypatch.setattr(
        service.settings, "BACKUP_DIR", str(tmp_path / "nothing-here"), raising=False
    )
    assert service.list_backups() == []


@pytest.mark.asyncio
async def test_backups_are_owner_only(client, make_admin, mint_token):
    for role in ("viewer", "support"):
        admin = await make_admin(email=f"{role}-backup@example.com", role=role)
        token = await mint_token(admin)

        listed = await client.get("/api/v1/admin/backups", headers=auth_header(token))
        created = await client.post("/api/v1/admin/backups", headers=auth_header(token))

        assert listed.status_code == 403, listed.text
        assert created.status_code == 403, created.text
        assert listed.json()["error_code"] == "INSUFFICIENT_ADMIN_ROLE"


@pytest.mark.asyncio
async def test_a_non_admin_cannot_reach_backups_at_all(client, make_user, mint_token):
    user = await make_user(email="nosybackup@example.com")
    token = await mint_token(user)

    response = await client.get("/api/v1/admin/backups", headers=auth_header(token))

    assert response.status_code in (401, 403), response.text


@pytest.mark.asyncio
async def test_an_owner_sees_the_listing(client, make_admin, mint_token, tmp_path, monkeypatch):
    monkeypatch.setattr(service.settings, "BACKUP_DIR", str(tmp_path), raising=False)
    admin = await make_admin(email="ownerbackup@example.com", role="owner")
    token = await mint_token(admin)

    response = await client.get("/api/v1/admin/backups", headers=auth_header(token))

    assert response.status_code == 200, response.text
    assert response.json()["items"] == []


@pytest.mark.asyncio
async def test_a_missing_pg_dump_is_explained_not_a_500(
    client, make_admin, mint_token, monkeypatch
):
    """
    The running container predates the Dockerfile change until it is rebuilt,
    so this is the likely first response in production -- it needs to say what
    to do rather than "internal server error".
    """
    monkeypatch.setattr(service.shutil, "which", lambda _: None)
    admin = await make_admin(email="nopgdump@example.com", role="owner")
    token = await mint_token(admin)

    response = await client.post("/api/v1/admin/backups", headers=auth_header(token))

    assert response.status_code == 503, response.text
    assert response.json()["error_code"] == "PG_DUMP_MISSING"
    assert "rebuild" in response.json()["message"].lower()


def test_an_unrelated_file_inside_the_directory_is_still_refused(tmp_path, monkeypatch):
    """
    What the name pattern is actually for.

    Traversal is caught by the containment check whether or not the pattern
    exists, so the pattern only earns its place here: a file that really does
    sit inside the backup directory, passes containment, and is not a dump.
    Without the pattern this would be served.
    """
    monkeypatch.setattr(service.settings, "BACKUP_DIR", str(tmp_path), raising=False)
    (tmp_path / "notes.txt").write_text("unrelated")
    (tmp_path / ".env").write_text("SECRET=1")

    for name in ("notes.txt", ".env"):
        with pytest.raises(Exception) as caught:
            service.path_for_download(name)
        assert getattr(caught.value, "status_code", None) == 400
