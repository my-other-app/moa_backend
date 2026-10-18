"""The renderer's image caches: by bytes, and never a failure.

`objects.get_bytes` and `documents.fetch_bytes` were `lru_cache(maxsize=64)`:
64 entries of any size, and a None kept like any other answer -- one slow
reply from S3 and a club's logo was blank on every ticket until the process
restarted.
"""
import io
import urllib.request

import pytest
from botocore.exceptions import ClientError

from app.api.events import documents
from app.config import settings
from app.core.storage import objects
from app.core.storage.objects import fetch as bucket_fetch  # the real one; the suite stubs it

KEY = "templates/tickets/logos/logo.png"


class FakeBucket:
    def __init__(self, data: dict):
        self.data = data
        self.fail_next: list[Exception] = []
        self.reads: list[str] = []

    def download_fileobj(self, bucket, key, buf):
        self.reads.append(key)
        if self.fail_next:
            raise self.fail_next.pop(0)
        if key not in self.data:
            raise ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")
        buf.write(self.data[key])


@pytest.fixture
def bucket(monkeypatch):
    fake = FakeBucket({KEY: b"\x89PNG logo"})
    monkeypatch.setattr(objects, "fetch", bucket_fetch)
    monkeypatch.setattr(objects, "client", lambda: fake)
    objects.cache_clear()
    yield fake
    objects.cache_clear()


def test_a_failed_read_is_not_kept(bucket):
    bucket.fail_next.append(TimeoutError("S3 took too long"))

    assert objects.get_bytes(KEY) is None
    # The next render asks again, and gets the logo.
    assert objects.get_bytes(KEY) == b"\x89PNG logo"
    assert objects.get_bytes(KEY) == b"\x89PNG logo"
    assert bucket.reads == [KEY, KEY]


def test_a_missing_key_is_remembered_for_a_while(bucket):
    assert objects.get_bytes("templates/tickets/logos/gone.png") is None
    assert objects.get_bytes("templates/tickets/logos/gone.png") is None
    assert bucket.reads == ["templates/tickets/logos/gone.png"]


def test_the_cache_is_capped_by_bytes(bucket, monkeypatch):
    monkeypatch.setattr(settings, "RENDER_IMAGE_CACHE_MB", 1)
    monkeypatch.setattr(settings, "RENDER_IMAGE_CACHE_ITEM_MB", 1)
    big = 400 * 1024
    for i in range(4):
        bucket.data[f"k{i}"] = bytes([i]) * big
        assert objects.get_bytes(f"k{i}") is not None

    cached = objects._images
    assert cached.size <= 1024 * 1024 and len(cached) == 2
    assert "k0" not in cached and "k3" in cached  # the least recently used went first


def test_one_object_over_the_item_cap_is_never_kept(bucket, monkeypatch):
    monkeypatch.setattr(settings, "RENDER_IMAGE_CACHE_ITEM_MB", 1)
    bucket.data["huge"] = b"x" * (2 * 1024 * 1024)

    assert objects.get_bytes("huge") is not None
    assert objects.get_bytes("huge") is not None
    assert bucket.reads.count("huge") == 2


def test_one_of_our_links_is_read_by_its_key(bucket):
    """The same logo signed at two times is one cached copy, and a link that
    has expired still reads."""
    link = f"https://{settings.S3_BUCKET}.s3.amazonaws.com/{KEY}?Signature=old&Expires=1"

    assert documents.fetch_bytes(link) == b"\x89PNG logo"
    assert documents.fetch_bytes(link.replace("old", "new")) == b"\x89PNG logo"
    assert bucket.reads == [KEY]


def test_an_outside_image_that_failed_is_asked_for_again(monkeypatch):
    calls = []

    class Answer(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def urlopen(url, timeout=None):
        calls.append(url)
        if len(calls) == 1:
            raise TimeoutError("slow")
        return Answer(b"\x89PNG remote")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    url = "https://cdn.example.com/a-logo-for-a-cache-test.png"

    assert documents.fetch_bytes(url) is None
    assert documents.fetch_bytes(url) == b"\x89PNG remote"
    assert documents.fetch_bytes(url) == b"\x89PNG remote"
    assert len(calls) == 2


def test_only_https_is_fetched_from_outside(monkeypatch):
    """A design names an address; the renderer should not be the one that
    asks an internal service for it."""
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: pytest.fail("fetched"))

    assert documents.fetch_bytes("http://169.254.169.254/latest/meta-data/") is None
    assert documents.fetch_bytes("file:///etc/passwd") is None


def test_the_poster_on_a_document_is_the_large_one_not_the_original():
    from types import SimpleNamespace

    from app.api.events.tickets import poster_url

    event = SimpleNamespace(poster={"original": "o", "large": "l", "medium": "m", "thumbnail": "t"})
    assert poster_url(event) == "l"
    assert poster_url(SimpleNamespace(poster={"original": "o", "medium": "m"})) == "m"
