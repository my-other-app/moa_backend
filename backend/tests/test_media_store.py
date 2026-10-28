"""The media bucket on a store of our own (app/core/storage/clients.py).

Garage on the production server: the API reaches it at http://media:3900,
browsers at https://media.myotherapp.in, path style, SigV4 in its own region
(garage). Unset, every media client is made exactly as before. Off-site
backups, the club mailbox's inbound bucket and SES stay on AWS either way.

No network: requests are caught before they leave (botocore's before-send
event) and links are signed locally.
"""
from functools import lru_cache
from urllib.parse import parse_qs, urlsplit

import pytest
from botocore.awsrequest import AWSResponse

from app.api.backups import offsite
from app.api.club_mailbox import providers, store
from app.api.emails import service as emails
from app.config import settings
from app.core.email import transport
from app.core.storage import bucket, clients, fields, objects

INTERNAL = "http://media:3900"
PUBLIC = "https://media.myotherapp.in"
BUCKET = "moa-media"
KEY = f"clubs/logos/v-{'a' * 32}.png"


@pytest.fixture
def fresh_clients(monkeypatch):
    """Clients made from the settings as they are now, as a process starting
    with them would make them; the suite's own come back afterwards."""
    monkeypatch.setattr(clients, "_cached", lru_cache(maxsize=2)(lambda public: clients.build(public=public)))
    fields._signed.clear()
    yield
    fields._signed.clear()


@pytest.fixture
def garage(monkeypatch, fresh_clients):
    monkeypatch.setattr(settings, "S3_ENDPOINT_URL", INTERNAL)
    monkeypatch.setattr(settings, "S3_PUBLIC_URL", PUBLIC)
    monkeypatch.setattr(settings, "S3_REGION", "garage")
    monkeypatch.setattr(settings, "S3_BUCKET", BUCKET)
    monkeypatch.setattr(settings, "S3_BASE_PATH", "")
    monkeypatch.setattr(settings, "MEDIA_CDN_URL", "")
    monkeypatch.setattr(fields, "_SIGNER", clients.media_client(public=True))


class _Body:
    def __init__(self, data: bytes = b""):
        self.data = data

    def stream(self, **kwargs):
        yield self.data


LISTING = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
    b"<Name>moa-media</Name><Prefix>clubs/</Prefix><KeyCount>0</KeyCount><IsTruncated>false</IsTruncated>"
    b"</ListBucketResult>"
)


@pytest.fixture
def sent(garage):
    """Every request the internal client makes: answered 200 here, never sent."""
    requests = []

    def catch(request, **kwargs):
        requests.append(request)
        return AWSResponse(request.url, 200, {}, _Body(LISTING if "list-type=2" in request.url else b""))

    client = clients.media_client()
    client.meta.events.register("before-send.s3", catch)
    yield requests
    client.meta.events.unregister("before-send.s3", catch)


# ─── Unset: as before ─────────────────────────────────────────────────────


def test_unset_every_media_client_is_made_exactly_as_before(monkeypatch):
    made = []
    monkeypatch.setattr(clients.boto3, "client", lambda *args, **kwargs: made.append((args, kwargs)))
    for name in ("S3_ENDPOINT_URL", "S3_PUBLIC_URL", "S3_REGION"):
        monkeypatch.setattr(settings, name, "")
    clients.build()
    clients.build(public=True)
    before = (("s3",), {"aws_access_key_id": settings.S3_ACCESS_KEY, "aws_secret_access_key": settings.S3_SECRET_KEY})
    assert made == [before, before]


def test_a_region_alone_is_passed_on_and_nothing_else(monkeypatch):
    made = []
    monkeypatch.setattr(clients.boto3, "client", lambda *args, **kwargs: made.append(kwargs))
    monkeypatch.setattr(settings, "S3_ENDPOINT_URL", "")
    monkeypatch.setattr(settings, "S3_PUBLIC_URL", "")
    monkeypatch.setattr(settings, "S3_REGION", "ap-south-1")
    clients.build(public=True)
    assert made == [{"aws_access_key_id": settings.S3_ACCESS_KEY, "aws_secret_access_key": settings.S3_SECRET_KEY,
                     "region_name": "ap-south-1"}]


def test_one_internal_client_and_one_signer_for_the_whole_process():
    """Made once, shared by every image and file column, the bucket's plain
    objects and the mailbox's files; signing has its own."""
    from sqlalchemy.orm import configure_mappers

    from app.db.base import AbstractSQLModel

    configure_mappers()
    internal = clients.media_client()
    columns = [c.type for t in AbstractSQLModel.metadata.tables.values() for c in t.columns if hasattr(c.type, "s3_client")]
    assert columns and all(t.s3_client is internal for t in columns)
    assert objects.client() is internal and clients.media_client(public=False) is internal
    assert fields._SIGNER is clients.media_client(public=True) is not internal
    # The suite sets no endpoint: AWS S3.
    assert "amazonaws.com" in internal.meta.endpoint_url and "amazonaws.com" in fields._SIGNER.meta.endpoint_url


# ─── Set: our own store ───────────────────────────────────────────────────


def test_with_an_endpoint_every_client_is_path_style_sigv4_in_its_region(garage):
    for public, url in ((False, INTERNAL), (True, PUBLIC)):
        client = clients.media_client(public=public)
        assert client.meta.endpoint_url == url
        assert client.meta.region_name == "garage"
        assert client.meta.config.signature_version == "s3v4"
        assert client.meta.config.s3 == {"addressing_style": "path"}
        assert client.meta.config.request_checksum_calculation == "when_required"


def test_uploads_heads_listings_and_deletes_go_to_the_internal_endpoint(garage, sent):
    objects.put_bytes(KEY, b"png-bytes", "image/png")
    clients.media_client().head_object(Bucket=BUCKET, Key=KEY)
    assert objects.list_keys("clubs/") == []
    clients.media_client().delete_object(Bucket=BUCKET, Key=KEY)

    assert [(r.method, r.url.split("?")[0]) for r in sent] == [
        ("PUT", f"{INTERNAL}/{BUCKET}/{KEY}"),
        ("HEAD", f"{INTERNAL}/{BUCKET}/{KEY}"),
        ("GET", f"{INTERNAL}/{BUCKET}"),
        ("DELETE", f"{INTERNAL}/{BUCKET}/{KEY}"),
    ]
    for request in sent:
        auth = request.headers["Authorization"].decode()
        assert auth.startswith("AWS4-HMAC-SHA256 ") and "/garage/s3/aws4_request" in auth
    # No checksum trailer on a plain upload (boto3 1.36's default): only where S3 demands one.
    assert b"aws-chunked" not in (sent[0].headers.get("Content-Encoding") or b"")
    assert "x-amz-trailer" not in {k.lower() for k in sent[0].headers}


def test_signed_links_are_for_the_public_host(garage):
    for url in (fields.signed_url(KEY, public=True), fields.signed_url(KEY), objects.presigned_url(KEY)):
        parts = urlsplit(url)
        assert f"{parts.scheme}://{parts.netloc}" == PUBLIC
        assert parts.path == f"/{BUCKET}/{KEY}"
        query = parse_qs(parts.query)
        assert query["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
        assert query["X-Amz-Credential"][0].endswith("/garage/s3/aws4_request")
        assert query["X-Amz-SignedHeaders"] == ["host"]


def test_without_a_public_address_links_are_for_the_endpoint(garage, monkeypatch):
    monkeypatch.setattr(settings, "S3_PUBLIC_URL", "")
    assert clients.build(public=True).meta.endpoint_url == INTERNAL


def test_a_signed_link_is_read_back_by_its_key(garage):
    """The renderer, the doc cache and old template configs are handed links
    and read them by key (never over https through the proxy)."""
    assert bucket.key_from_url(fields.signed_url(KEY)) == KEY
    assert bucket.key_from_url(f"{INTERNAL}/{BUCKET}/{KEY}") == KEY
    assert bucket.key_from_url(f"{PUBLIC}/{BUCKET}/clubs/team/a%20b.png?X-Amz-Signature=x") == "clubs/team/a b.png"
    assert bucket.key_from_url(f"{PUBLIC}/another-bucket/{KEY}") is None
    assert bucket.key_from_url(f"https://elsewhere.test/{BUCKET}/{KEY}") is None
    assert bucket.key_from_url(f"{PUBLIC}/{BUCKET}/") is None


def test_a_link_on_our_store_is_unknown_on_aws(monkeypatch):
    monkeypatch.setattr(settings, "S3_ENDPOINT_URL", "")
    monkeypatch.setattr(settings, "S3_PUBLIC_URL", "")
    assert bucket.key_from_url(f"{PUBLIC}/{settings.S3_BUCKET}/{KEY}") is None


# ─── What stays on AWS ────────────────────────────────────────────────────


def test_backups_the_mailbox_inbound_bucket_and_ses_stay_on_aws(garage, monkeypatch):
    monkeypatch.setattr(providers, "_client", None)
    monkeypatch.setattr(providers, "_client_key", None)
    for client in (offsite.client(), providers._s3(), transport._new_ses_client("ap-south-1", "k", "s")):
        assert client.meta.endpoint_url.endswith(".amazonaws.com"), client.meta.endpoint_url
        assert client.meta.region_name != "garage"
        assert client.meta.config.s3 is None or client.meta.config.s3.get("addressing_style") != "path"


def test_mailbox_files_carry_no_sse_header_on_our_store(garage, monkeypatch):
    """Garage implements no SSE-S3: the header is AWS's alone."""
    extra = []

    class Client:
        def upload_fileobj(self, fileobj, bucket_name, key, ExtraArgs=None):
            extra.append(ExtraArgs)

    monkeypatch.setattr(objects, "client", lambda: Client())
    store.put("mailbox/1/2/0", b"raw", "message/rfc822")
    monkeypatch.setattr(settings, "S3_ENDPOINT_URL", "")
    store.put("mailbox/1/2/1", b"raw", "message/rfc822")
    assert extra == [
        {"ContentType": "application/octet-stream"},
        {"ContentType": "application/octet-stream", "ServerSideEncryption": "AES256"},
    ]


# ─── S3_BASE_PATH ─────────────────────────────────────────────────────────


def test_with_an_empty_base_path_keys_start_at_the_buckets_root(monkeypatch):
    monkeypatch.setattr(settings, "S3_BASE_PATH", "")
    assert emails.image_prefix(event_id=7) == "events/7/layout/"
    assert emails.image_prefix(club_id=3) == "clubs/3/email/"
    assert bucket.prefix("docs") == "docs/"


def test_a_base_path_still_goes_in_front(monkeypatch):
    monkeypatch.setattr(settings, "S3_BASE_PATH", "media/")
    assert emails.image_prefix(event_id=7) == "media/events/7/layout/"
    assert emails.image_prefix(club_id=3) == "media/clubs/3/email/"
