"""scripts/media_sync.py, against moto's S3: the media bucket copied into the
off-site backup bucket under its own prefix, encrypted, copy only (nothing
deleted off the box), only what is missing or changed in size; off unless
both settings say so; a failed object fails the run without stopping the
rest. And the off-site client with an endpoint of its own (an S3-compatible
store other than AWS).
"""

import io
from contextlib import redirect_stdout

import pytest

from app.api.backups import offsite
from app.config import settings
from scripts import media_sync

MEDIA, BACKUPS = "moa-media-test", "moa-offsite-test"


@pytest.fixture
def s3(monkeypatch):
    moto = pytest.importorskip("moto")
    import boto3

    for name in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_PROFILE",
    ):
        monkeypatch.delenv(name, raising=False)
    with moto.mock_aws():
        client = boto3.client(
            "s3",
            region_name="ap-south-1",
            aws_access_key_id="AKIAMOTO",
            aws_secret_access_key="moto",
        )
        for bucket in (MEDIA, BACKUPS):
            client.create_bucket(
                Bucket=bucket,
                CreateBucketConfiguration={"LocationConstraint": "ap-south-1"},
            )
        monkeypatch.setattr(settings, "S3_BUCKET", MEDIA)
        monkeypatch.setattr(settings, "BACKUP_S3_BUCKET", BACKUPS)
        monkeypatch.setattr(settings, "BACKUP_S3_PREFIX", "backups/")
        monkeypatch.setattr(settings, "BACKUP_S3_REGION", "ap-south-1")
        monkeypatch.setattr(settings, "BACKUP_S3_SSE", "AES256")
        monkeypatch.setattr(settings, "BACKUP_S3_ENDPOINT_URL", "")
        monkeypatch.setattr(settings, "MEDIA_SYNC", True)
        monkeypatch.setattr(settings, "MEDIA_SYNC_PREFIX", "media/")
        yield client


def _run(s3, **kwargs) -> tuple[int, str]:
    out = io.StringIO()
    with redirect_stdout(out):
        code = media_sync.run(src=s3, dst=s3, **kwargs)
    return code, out.getvalue()


def _put(
    s3, key: str, body: bytes, bucket: str = MEDIA, content_type: str = "image/png"
):
    s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType=content_type)


def _keys(s3, bucket: str = BACKUPS) -> dict:
    return {
        o["Key"]: o["Size"]
        for o in s3.list_objects_v2(Bucket=bucket).get("Contents", [])
    }


def test_it_is_off_unless_both_settings_say_so(s3, monkeypatch):
    _put(s3, "clubs/logos/v-a.png", b"a")
    monkeypatch.setattr(settings, "MEDIA_SYNC", False)
    assert _run(s3) == (0, "media sync: off (APP_MEDIA_SYNC is not true)\n")
    monkeypatch.setattr(settings, "MEDIA_SYNC", True)
    monkeypatch.setattr(settings, "BACKUP_S3_BUCKET", "")
    code, out = _run(s3)
    assert code == 0 and "off (APP_BACKUP_S3_BUCKET is not set)" in out
    assert _keys(s3) == {}


def test_a_prefix_inside_the_backups_one_is_refused(s3, monkeypatch):
    monkeypatch.setattr(settings, "MEDIA_SYNC_PREFIX", "backups/media/")
    code, out = _run(s3)
    assert code == 0 and "overlaps the backups' prefix" in out and _keys(s3) == {}


def test_it_copies_what_is_missing_or_changed_encrypted_and_never_deletes(s3):
    _put(s3, "clubs/logos/v-a.png", b"aaaa")
    _put(s3, "events/posters/v-b.jpg", b"bbbbbb", content_type="image/jpeg")
    _put(s3, "tickets/c.pdf", b"cc", content_type="application/pdf")
    # Already there, same size: left alone. Changed size: copied again.
    _put(s3, "media/clubs/logos/v-a.png", b"AAAA", bucket=BACKUPS)
    _put(s3, "media/tickets/c.pdf", b"c", bucket=BACKUPS)
    # Gone from the box: kept in the copy. And the dumps are not touched.
    _put(s3, "media/old/removed.png", b"zz", bucket=BACKUPS)
    _put(s3, "backups/daily/backup-20261115-213000.dump", b"PGDMP", bucket=BACKUPS)

    code, out = _run(s3)
    assert code == 0, out
    assert (
        "3 objects in the media bucket; 2 copied" in out
        and "0 failed, 0 still to copy" in out
    )
    assert "MEDIA SYNC OK" in out
    assert "v-b.jpg" not in out and "tickets" not in out  # counts, never keys
    keys = _keys(s3)
    assert (
        keys["media/events/posters/v-b.jpg"] == 6 and keys["media/tickets/c.pdf"] == 2
    )
    assert (
        keys["media/old/removed.png"] == 2
        and "backups/daily/backup-20261115-213000.dump" in keys
    )
    assert (
        s3.get_object(Bucket=BACKUPS, Key="media/clubs/logos/v-a.png")["Body"].read()
        == b"AAAA"
    )
    head = s3.head_object(Bucket=BACKUPS, Key="media/events/posters/v-b.jpg")
    assert (
        head["ServerSideEncryption"] == "AES256" and head["ContentType"] == "image/jpeg"
    )
    assert head["Metadata"]["source-etag"] == s3.head_object(
        Bucket=MEDIA, Key="events/posters/v-b.jpg"
    )["ETag"].strip('"')

    # A second run has nothing to do.
    code, out = _run(s3)
    assert code == 0 and "0 copied" in out


def test_a_dry_run_and_a_limit(s3):
    for n in range(5):
        _put(s3, f"clubs/logos/v-{n}.png", b"x" * (n + 1))
    code, out = _run(s3, dry_run=True)
    assert code == 0 and "would copy 5" in out and _keys(s3) == {}
    code, out = _run(s3, limit=2)
    assert (
        code == 0
        and "2 copied" in out
        and "3 still to copy" in out
        and len(_keys(s3)) == 2
    )


def test_one_failed_object_fails_the_run_and_the_rest_still_go(s3, monkeypatch):
    for name in ("a", "b", "c"):
        _put(s3, f"clubs/logos/v-{name}.png", b"x")
    real = media_sync.copy_one

    def flaky(src, dst, key):
        if key.endswith("v-b.png"):
            raise OSError("disk went away")
        real(src, dst, key)

    monkeypatch.setattr(media_sync, "copy_one", flaky)
    code, out = _run(s3)
    assert (
        code == 1
        and "2 copied" in out
        and "1 failed" in out
        and "media sync FAILED" in out
    )
    assert "an object was not copied: OSError" in out and "disk went away" not in out


def test_no_encryption_header_when_the_store_encrypts_by_itself(s3, monkeypatch):
    monkeypatch.setattr(settings, "BACKUP_S3_SSE", "none")
    assert offsite.encryption() == {}
    _put(s3, "clubs/logos/v-a.png", b"a")
    sent = []
    real_upload = s3.upload_fileobj

    def upload(body, bucket, key, ExtraArgs=None):  # noqa: N803 -- boto3's name
        sent.append(ExtraArgs)
        return real_upload(body, bucket, key, ExtraArgs=ExtraArgs)

    monkeypatch.setattr(s3, "upload_fileobj", upload)
    assert _run(s3)[0] == 0
    assert (
        sent
        and "ServerSideEncryption" not in sent[0]
        and sent[0]["ContentType"] == "image/png"
    )


def test_the_offsite_client_uses_its_own_endpoint_path_style(monkeypatch):
    monkeypatch.setattr(
        settings,
        "BACKUP_S3_ENDPOINT_URL",
        "https://s3.eu-central-003.backblazeb2.example",
    )
    monkeypatch.setattr(settings, "BACKUP_S3_ACCESS_KEY", "KEYID000000000000001")
    monkeypatch.setattr(
        settings, "BACKUP_S3_SECRET_KEY", "not-a-real-secret-0000000000000000"
    )
    client = offsite.client()
    assert client.meta.endpoint_url == "https://s3.eu-central-003.backblazeb2.example"
    assert client.meta.config.s3["addressing_style"] == "path"
    monkeypatch.setattr(settings, "BACKUP_S3_ENDPOINT_URL", "")
    assert "amazonaws.com" in offsite.client().meta.endpoint_url
